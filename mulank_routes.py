"""Routes for the Mulank payment flow. Mounted by api.py with one include_router line.

`api` is imported inside handlers (not at module top) because api.py includes this
router; it also keeps api.RZP_SECRET / api.rzp_client() monkeypatchable in tests.
DEMO_MODE is deliberately never read here.
"""
import html as _html
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

import db_v2
import mulank_content
import mulank_form
import mulank_fulfilment
import mulank_payment as mp
import mulank_preorder
import mulank_recovery
import numerology
import numerology_landing
import numerology_preview
import numerology_report
import numerology_viewer
import payments
import ratelimit
import reports_v2

logger = logging.getLogger("axtroshastra")
router = APIRouter()


FULFIL = mulank_fulfilment.schedule      # renders and caches the PDF after verified payment; no WhatsApp is sent


class MulankOrderIn(BaseModel):
    report_id: str | None = None        # nothing else is read: price comes from the server


def _settle_in_flight(api, rid: str) -> bool:
    """A payment for this report is already captured at Razorpay but not yet recorded here: record it now (same
    verification as always) instead of letting the customer pay again. True once the report is paid."""
    try:
        res = mulank_recovery.recover(api.rzp_client(), limit=5, rid=rid)
    except Exception:
        return False
    for paid_rid in res["paid"]:
        FULFIL(paid_rid, None)
    rec = _load(rid)
    return bool(rec and rec["paid"])


def _http(e: mp.MulankPaymentError):
    return HTTPException(e.http, e.code)


@router.post("/api/mulank/order")
def mulank_order(body: MulankOrderIn):
    import api
    conn = db_v2.get_conn()
    try:
        try:
            out = mp.create_order(conn, lambda: api.rzp_client(), body.report_id or "")
            if "razorpay_order_id" in out:
                out["key_id"] = api.RZP_KEY          # public key id, by design; the secret never leaves the server
            return out
        except mp.MulankPaymentError as e:
            conn.rollback()
            if e.code == "payment_processing" and _settle_in_flight(api, body.report_id or ""):
                return {"already_paid": True}
            raise _http(e)
    finally:
        conn.close()


@router.post("/api/mulank/verify")
def mulank_verify(body: dict, background_tasks: BackgroundTasks):
    import api
    pid = (body.get("razorpay_payment_id") or "").strip()
    oid = (body.get("razorpay_order_id") or "").strip()
    sig = (body.get("razorpay_signature") or "").strip()
    if not (pid and oid and sig):
        raise HTTPException(400, "missing_payment_fields")
    if not api.RZP_SECRET:
        raise HTTPException(503, "payment_not_configured")
    if not mp.signature_ok(api.RZP_SECRET, oid, pid, sig):
        raise HTTPException(400, "invalid_signature")
    try:
        ent = api.rzp_client().payment.fetch(pid)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[mulank] payment fetch failed: %s", e.__class__.__name__)
        raise HTTPException(502, "verification_unavailable")   # safe to retry; nothing unlocked
    conn = db_v2.get_conn()
    try:
        try:
            out = mp.settle(conn, ent, order_id=oid, payment_id=pid)
        except mp.MulankPaymentError as e:
            conn.rollback()
            raise _http(e)
    finally:
        conn.close()
    if out["deliver"]:
        FULFIL(out["report_id"], background_tasks)
    return {"ok": out["status"] == "paid", "status": out["status"], "report_id": out["report_id"]}


@router.post("/api/mulank/webhook")
async def mulank_webhook(request: Request, background_tasks: BackgroundTasks):
    import api
    raw = await request.body()
    if not api.RZP_WEBHOOK_SECRET:
        raise HTTPException(503, "webhook_not_configured")
    if not mp.webhook_signature_ok(api.RZP_WEBHOOK_SECRET, raw,
                                   request.headers.get("X-Razorpay-Signature", "")):
        raise HTTPException(400, "bad_signature")
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "bad_payload")
    if event.get("event") != "payment.captured":
        return {"ok": True}
    ent = (((event.get("payload") or {}).get("payment") or {}).get("entity")) or {}
    oid, pid = ent.get("order_id") or "", ent.get("id") or ""
    if not (oid and pid):
        return {"ok": True}
    conn = db_v2.get_conn()
    try:
        try:
            out = mp.settle(conn, ent, order_id=oid, payment_id=pid,
                            event_id=payments.event_id_of(event), event_type="payment.captured")
        except mp.MulankPaymentError as e:
            conn.rollback()
            if e.code == "order_not_found":     # not a Mulank order: the shared webhook's job
                return {"ok": True}
            logger.error("[mulank] webhook rejected code=%s", e.code)
            return {"ok": False, "error": e.code}   # 200 so Razorpay does not retry a bad payment forever
    finally:
        conn.close()
    if out["deliver"]:
        FULFIL(out["report_id"], background_tasks)
    return {"ok": True}


# =============================================================== customer journey (Piece 4)
CONSENT_V = "2026-11-mulank"                  # policy/consent text version stored with each lead
WAITLIST_CONSENT_TEXT = ""                    # owner-supplied wording; the waitlist box stays hidden while empty
RID_RE = re.compile(r"[A-Za-z0-9_-]{8,40}")
_PAGES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages")

_hits: dict = {}
_hits_lock = threading.Lock()


def _limit(request: Request, bucket: str, cap: int, per_min: int) -> None:
    """Small per-IP token bucket for these routes only (the shared limiter's path table is untouched).
    Honours RATE_LIMIT_ENABLED like the shared one."""
    if os.getenv("RATE_LIMIT_ENABLED", "1") != "1":
        return
    ip, now = ratelimit._client_ip(request), time.monotonic()
    with _hits_lock:
        if len(_hits) > 20000:
            _hits.clear()
        tokens, last = _hits.get((bucket, ip), (cap, now))
        tokens = min(cap, tokens + (now - last) * per_min / 60)
        if tokens < 1:
            _hits[(bucket, ip)] = (tokens, now)
            raise HTTPException(429, "rate_limited", headers={"Retry-After": "30"})
        _hits[(bucket, ip)] = (tokens - 1, now)


def _private(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _not_found():
    return _private(HTMLResponse("<!doctype html><meta charset=utf-8><meta name=robots content=noindex>"
                                 "<title>Not found</title><p style='font:16px system-ui;padding:40px'>This page was not found.</p>",
                                 status_code=404))


def _load(rid: str):
    """The Mulank report for rid (or None). Same answer for 'no such report' and 'someone else's product'."""
    if not RID_RE.fullmatch(rid or ""):
        return None
    conn = db_v2.get_conn()
    try:
        rec = reports_v2.read_report(conn, rid)
    finally:
        conn.close()
    if not rec or rec.get("product") != mp.PRODUCT:
        return None
    return rec


class MulankIn(BaseModel):
    name: str | None = None
    dob: str | None = None
    gender: str | None = None
    country_code: str | None = "+91"
    phone: str | None = None
    consent_terms: bool = False
    marketing_optin: bool = False
    website: str | None = None                # honeypot: real people never fill this
    captcha_token: str | None = None


@router.post("/api/mulank")
def mulank_create(inp: MulankIn, request: Request):
    """Validate, calculate, save the lead and report, and return an opaque preview link.
    Nothing personal is logged or returned beyond the new report id."""
    import api
    _limit(request, "create", 6, 6)
    if inp.website:
        raise HTTPException(400, "invalid")
    if not ratelimit.captcha_ok(inp.captcha_token or "", ""):
        raise HTTPException(400, "captcha_failed")
    try:
        f = mulank_form.validate(inp.model_dump())
    except mulank_form.FormError as e:
        raise HTTPException(422, e.detail())
    E = numerology.compute_mulank(f["day"], f["month"], f["year"])
    ready = mulank_content.pair_readiness(f["day"], f["month"], f["year"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "product": mp.PRODUCT, "name": f["name"], "dob": f["dob_iso"],
        "numbers": {"mulank": E["mulank"]["number"], "bhagyank": E["bhagyank"]["number"], "pair_key": E["pair_key"]},
        "meta": {"variant": "/en/numerology", "lang": "english", "consent_ts": now, "consent_v": CONSENT_V,
                 "marketing_optin": f["marketing_optin"], "marketing_optin_ts": now if f["marketing_optin"] else None,
                 "gender_choice": f["gender"], "content_version": ready["content_version"],
                 "eligible_at_creation": ready["eligible"]},
    }
    subject = {"role": "self", "name": f["name"],
               "gender": f["gender"] if f["gender"] in mulank_form.DB_GENDERS else None,
               "dob": f["dob_iso"], "tob": None, "time_quality": None,
               "birth_place": None, "birth_lat": None, "birth_lon": None, "birth_tz": None}
    rid = secrets.token_urlsafe(12)
    try:
        conn = db_v2.get_conn()
        try:
            uid = reports_v2.save_report(conn, rid, report, [subject], product=mp.PRODUCT, phone=f["phone"])
            try:
                api._identify_visitor(conn, request, uid)
            except Exception:
                logger.warning("[mulank] visitor link skipped")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("[mulank] save failed: %s", e.__class__.__name__)
        raise HTTPException(503, "unavailable")
    logger.info("[mulank] created pair=%s eligible=%s", E["pair_key"], ready["eligible"])
    if not ready["eligible"]:
        logger.info(mulank_content.gap_log_line(ready))
    return _private(Response(json.dumps({"report_id": rid, "preview_url": f"/en/numerology/preview/{rid}"}),
                             media_type="application/json"))


def _preview_for(rec: dict, rid: str, absolute_cta: bool = False):
    p = rec["payload"]
    y, m, d = (int(x) for x in p["dob"].split("-"))
    cta = None
    base = (os.getenv("PUBLIC_BASE_URL", "") or "https://www.axtroshastra.com").rstrip("/")   # same default as tracking.py
    if absolute_cta and base.startswith("https://"):
        cta = f"{base}/en/numerology/preview/{rid}"
    pre = mulank_preorder.config()
    refund = pre["refund_url"]
    if absolute_cta and refund.startswith("/"):               # a link inside a PDF must be absolute
        refund = base + refund
    return numerology_preview.build_preview(d, m, y, p.get("name") or "", cta_url=cta,
                                            waitlist_consent_text=WAITLIST_CONSENT_TEXT,
                                            preorder=pre if pre["enabled"] else None, refund_href=refund or None)


@router.get("/en/numerology/preview/{rid}.pdf", include_in_schema=False)
def mulank_preview_pdf(rid: str, request: Request):
    _limit(request, "pdf", 8, 8)
    rec = _load(rid)
    if not rec:
        return _not_found()
    try:
        r = _preview_for(rec, rid, absolute_cta=True)
        import pdfgen
        tag = "1" if r["eligible"] else ("p" + mulank_preorder.config()["version"] if r["preorder"] else "0")
        key = f"pv{tag}_{rid}"                                # never the bare report id (the PAID report's key); includes the promise version
        data = pdfgen.get_or_generate(key, r["html"])
    except Exception as e:
        logger.error("[mulank] preview pdf failed: %s", e.__class__.__name__)
        data = None
    if not data:
        return _private(Response(json.dumps({"detail": "pdf_unavailable"}), status_code=503, media_type="application/json"))
    resp = Response(data, media_type="application/pdf")
    resp.headers["Content-Disposition"] = 'attachment; filename="Axtroshastra-Numerology-Preview.pdf"'
    return _private(resp)


@router.get("/en/numerology/preview/{rid}", include_in_schema=False)
def mulank_preview(rid: str, request: Request):
    _limit(request, "view", 60, 60)
    rec = _load(rid)
    if not rec:
        return _not_found()
    if not rec["paid"] and _recover_one(rid, request):       # a payment that was captured but never confirmed
        rec = _load(rid) or rec
    if rec["paid"]:
        return _private(RedirectResponse(f"/en/numerology/report/{rid}", status_code=302))
    try:
        r = _preview_for(rec, rid)
        page = numerology_viewer.render_viewer(r["html"], rid, r["eligible"], r["preorder"])
    except Exception as e:
        logger.error("[mulank] preview render failed: %s", e.__class__.__name__)
        return _private(HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta name=robots content=noindex><title>Preview</title><p style='font:16px/1.5 system-ui;padding:40px;max-width:480px;margin:auto'>"
            "We could not prepare your preview just now. Please refresh this page in a minute, or "
            "<a href='/en/numerology'>start again</a>.</p>", status_code=503))
    return _private(HTMLResponse(page))


def _recover_one(rid: str, request: Request) -> bool:
    """Settle a captured-but-unconfirmed payment for this report (same verification as the dedicated routes)."""
    import api
    try:
        _limit(request, "recover", 12, 12)
        client = api.rzp_client() if (api.RZP_KEY and api.RZP_SECRET) else None
    except HTTPException:
        return False
    except Exception:
        return False
    if client is None:
        return False
    res = mulank_recovery.recover(client, limit=5, rid=rid)
    for paid_rid in res["paid"]:
        FULFIL(paid_rid, None)
    return res["recovered"] > 0


@router.get("/en/numerology/report/{rid}.pdf", include_in_schema=False)
def mulank_report_pdf(rid: str, request: Request):
    """The paid PDF. Only for a paid report; anything else looks exactly like 'no such page'."""
    _limit(request, "pdf", 8, 8)
    rec = _load(rid)
    if not rec or not rec["paid"]:
        return _not_found()
    import pdfgen
    data = pdfgen.get_cached(rid)
    if not data:
        mulank_fulfilment.run_fulfilment(rid)                   # retry in place; idempotent, never charges
        data = pdfgen.get_cached(rid)
    if not data:
        return _private(Response(json.dumps({"detail": "pdf_unavailable"}), status_code=503, media_type="application/json"))
    resp = Response(data, media_type="application/pdf")
    resp.headers["Content-Disposition"] = 'attachment; filename="Axtroshastra-Numerology-Report.pdf"'
    return _private(resp)


@router.get("/en/numerology/report/{rid}", include_in_schema=False)
def mulank_report(rid: str, request: Request):
    """The customer's paid report. Unpaid reports go back to the preview; nothing paid is ever shown otherwise."""
    _limit(request, "view", 60, 60)
    rec = _load(rid)
    if not rec:
        return _not_found()
    if not rec["paid"]:
        return _private(RedirectResponse(f"/en/numerology/preview/{rid}", status_code=302))
    state = mulank_fulfilment.get_state(rec["payload"])
    try:
        doc = mulank_fulfilment.render(rec)["html"]
    except (numerology_report.ReportNotAvailable, numerology_report.ContentMissing):
        state = mulank_fulfilment.run_fulfilment(rid)            # records content_unavailable; nothing unverified is shown
        return _private(HTMLResponse(numerology_viewer.render_pending_page(rid, state, mulank_preorder.get(rec["payload"]))))
    except Exception as e:
        logger.error("[mulank] report render failed: %s", e.__class__.__name__)
        return _private(HTMLResponse(numerology_viewer.render_pending_page(rid, {"status": "failed"}), status_code=503))
    if state.get("status") != mulank_fulfilment.READY:
        mulank_fulfilment.run_fulfilment(rid)                    # opportunistic retry on view; idempotent
    return _private(HTMLResponse(numerology_viewer.render_paid_viewer(doc, rid, state)))


@router.post("/api/mulank/report/{rid}/retry")
def mulank_report_retry(rid: str, request: Request):
    """Try fulfilment again for a PAID report. Idempotent; it can never charge or fulfil twice."""
    _limit(request, "retry", 6, 6)
    rec = _load(rid)
    if not rec or not rec["paid"]:
        raise HTTPException(404, "not found")
    state = mulank_fulfilment.run_fulfilment(rid)
    return _private(Response(json.dumps({"status": state.get("status")}), media_type="application/json"))


@router.on_event("startup")
def _start_mulank_recovery():
    """Background recovery sweep for Mulank payments (a no-op unless Razorpay is configured)."""
    import api
    if api.RZP_KEY and api.RZP_SECRET:
        mulank_recovery.start_loop(lambda: api.rzp_client(), mulank_fulfilment.schedule)


@router.get("/en/numerology", include_in_schema=False)
def mulank_landing():
    """Landing page. The offer blocks follow what can actually be bought: nothing (honest notice, no price), instant
    sale for combinations that are ready, or an owner-approved pre-order. The free preview is always first."""
    import api
    resp = api._serve_page_with_nav(os.path.join(_PAGES, "numerology.html"))
    body = resp.body.decode("utf-8") if hasattr(resp, "body") else None
    if body is None:
        return resp
    pre = mulank_preorder.config()
    state = "sale" if mulank_content.sale_open() else ("preorder" if pre["enabled"] else "off")
    body = body.replace("<!--MK_NOTICE-->", numerology_landing.hero_offer(state, pre), 1)
    body = body.replace("<!--MK_BTNNOTE-->", numerology_landing.btn_note(state), 1)
    body = body.replace("<!--MK_COMPARE-->", numerology_landing.compare_section(state, pre), 1)
    return HTMLResponse(body)

# ------------------------------------------------------------------ pre-order (OFF unless the owner configures it)
@router.post("/api/mulank/preorder")
def mulank_preorder_order(body: MulankOrderIn, request: Request):
    """Pre-order for a report whose content is not ready. Same verified payment as a normal order (amount, currency,
    order, captured status). Closed (409) unless MULANK_PREORDER, the delivery promise and the refund page are set,
    and closed for any pair that can be sold normally (use /api/mulank/order)."""
    import api
    _limit(request, "preorder", 12, 12)
    conn = db_v2.get_conn()
    try:
        try:
            out = mp.create_order(conn, lambda: api.rzp_client(), body.report_id or "", preorder=mulank_preorder.config())
            if "razorpay_order_id" in out:
                out["key_id"] = api.RZP_KEY
            return out
        except mp.MulankPaymentError as e:
            conn.rollback()
            if e.code == "payment_processing" and _settle_in_flight(api, body.report_id or ""):
                return {"already_paid": True}
            raise _http(e)
    finally:
        conn.close()


# ------------------------------------------------------------------ ops: see and fulfil paid orders (no personal data)
def _admin(request: Request):
    import api
    key = (api.STATS_KEY or "")
    if not key:
        raise HTTPException(503, "admin disabled")
    import hmac as _hmac
    if not _hmac.compare_digest(request.headers.get("X-Stats-Key", ""), key):
        raise HTTPException(403, "forbidden")
    _limit(request, "admin", 20, 20)


def _paid_orders(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT r.id, MAX(p.paid_at) FROM reports r JOIN payments p ON p.report_id=r.id "
                    "WHERE r.product=%s AND r.status='paid' GROUP BY r.id ORDER BY MAX(p.paid_at)", (mp.PRODUCT,))
        return cur.fetchall()


@router.get("/api/mulank/admin/orders")
def mulank_admin_orders(request: Request):
    """Paid Mulank orders by fulfilment state, with whether each could be fulfilled right now. Never returns names,
    phone numbers, dates of birth or the private report ids."""
    _admin(request)
    conn = db_v2.get_conn()
    try:
        rows, counts = [], {}
        for i, (rid, paid_at) in enumerate(_paid_orders(conn), 1):
            rec = reports_v2.read_report(conn, rid)
            st = mulank_fulfilment.get_state(rec["payload"]).get("status", "pending")
            ready_now = mp.readiness_for(rec)["eligible"]
            counts[st] = counts.get(st, 0) + 1
            rows.append({"n": i, "paid_at": str(paid_at) if paid_at else None, "pair": mp.readiness_for(rec)["pair_key"],
                         "preorder": bool(mulank_preorder.get(rec["payload"])), "state": st, "can_fulfil_now": bool(ready_now and st != "ready")})
        return {"total": len(rows), "counts": counts, "orders": rows}
    finally:
        conn.close()


@router.post("/api/mulank/admin/fulfil")
def mulank_admin_fulfil(request: Request):
    """Fulfil every paid order whose report can now be produced (content approved and complete). Idempotent."""
    _admin(request)
    conn = db_v2.get_conn()
    try:
        todo = []
        for rid, _ in _paid_orders(conn):
            rec = reports_v2.read_report(conn, rid)
            if mulank_fulfilment.get_state(rec["payload"]).get("status") != mulank_fulfilment.READY and mp.readiness_for(rec)["eligible"]:
                todo.append(rid)
    finally:
        conn.close()
    results = {}
    for rid in todo:
        st = mulank_fulfilment.run_fulfilment(rid).get("status")
        results[st] = results.get(st, 0) + 1
    return {"attempted": len(todo), "results": results}
