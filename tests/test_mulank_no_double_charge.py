"""A customer is never charged twice for one report, and the 12-page fallback or any sample content can never be
delivered. Razorpay is stubbed; the DB is the real throwaway MySQL."""
import hashlib
import hmac
import itertools
import pathlib
import re
from datetime import date

import pytest

import api
import db_v2
import mulank_content as mc
import mulank_fulfilment as mf
import mulank_routes
import numerology
import reports_v2

PIN = date(2026, 11, 3)
SECRET = "rzp_key_secret_double_test"
_ph = itertools.count(1)


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setattr(mc, "_today", lambda: PIN)


class Rzp:
    def __init__(self):
        self.created, self.status, self.captured, self.fetch_error = [], {}, {}, None
        outer = self

        class order:
            @staticmethod
            def create(d):
                oid = "order_" + d["receipt"][-20:] + "_%d" % len(outer.created)
                outer.created.append(d)
                outer.status[oid] = "created"
                return {"id": oid, "amount": d["amount"], "currency": d["currency"]}

            @staticmethod
            def fetch(oid):
                if outer.fetch_error:
                    raise outer.fetch_error
                return {"id": oid, "status": outer.status.get(oid, "created"), "amount": 29900, "currency": "INR"}

            @staticmethod
            def payments(oid):
                return {"items": outer.captured.get(oid, [])}

        class payment:
            @staticmethod
            def fetch(pid):
                return dict(outer.entity)
        self.order, self.payment, self.entity = order, payment, {}


@pytest.fixture
def rz(monkeypatch):
    s = Rzp()
    monkeypatch.setattr(api, "RZP_SECRET", SECRET)
    monkeypatch.setattr(api, "RZP_KEY", "rzp_test_key")
    monkeypatch.setattr(api, "rzp_client", lambda: s)
    return s


@pytest.fixture
def fulfilled(monkeypatch):
    calls = []
    monkeypatch.setattr(mulank_routes, "FULFIL", lambda rid, bg: calls.append(rid))
    return calls


@pytest.fixture
def eligible(monkeypatch):
    E = numerology.compute_mulank(29, 7, 1990, today=PIN)
    monkeypatch.setattr(mc, "REGISTRY", {k: mc.Entry(mc.OWNER_APPROVED, editor="E", approved_by="O", approved_on="2026-11-03")
                                         for k in mc.required_keys(E)})


@pytest.fixture
def preorder_on(monkeypatch):
    monkeypatch.setenv("MULANK_PREORDER", "1")
    monkeypatch.setenv("MULANK_DELIVERY_PROMISE", "within 21 days of your order")
    monkeypatch.setenv("MULANK_REFUND_URL", "/refund")


def _make(client):
    r = client.post("/api/mulank", json={"name": "Priya Sharma", "dob": "29/07/1990", "country_code": "+91",
                                         "phone": "92%08d" % (30000000 + next(_ph)), "consent_terms": True})
    assert r.status_code == 200, r.text
    return r.json()["report_id"]


def _paid(rid):
    conn = db_v2.get_conn()
    try:
        return reports_v2.read_report(conn, rid)["paid"]
    finally:
        conn.close()


def _captured_rows(rid):
    conn = db_v2.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM payments WHERE report_id=%s AND status='captured'", (rid,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def _ent(oid, **kw):
    e = {"id": "pay_" + oid[-24:], "order_id": oid, "amount": 29900, "currency": "INR", "status": "captured", "method": "upi"}
    e.update(kw)
    return e


# ------------------------------------------------------------------ one report, one payable order
def test_clicking_buy_twice_reuses_the_same_order_and_creates_only_one(client, rz, eligible):
    rid = _make(client)
    a = client.post("/api/mulank/order", json={"report_id": rid}).json()
    b = client.post("/api/mulank/order", json={"report_id": rid}).json()
    c = client.post("/api/mulank/order", json={"report_id": rid}).json()
    assert a["razorpay_order_id"] == b["razorpay_order_id"] == c["razorpay_order_id"] and len(rz.created) == 1
    assert a["amount"] == 29900 and a["currency"] == "INR" and a["key_id"] == "rzp_test_key"


def test_an_attempted_order_is_reused_too(client, rz, eligible):
    rid = _make(client)
    first = client.post("/api/mulank/order", json={"report_id": rid}).json()["razorpay_order_id"]
    rz.status[first] = "attempted"                                   # the customer opened checkout and tried a card
    assert client.post("/api/mulank/order", json={"report_id": rid}).json()["razorpay_order_id"] == first
    assert len(rz.created) == 1


def test_when_razorpay_cannot_be_asked_no_second_order_is_created(client, rz, eligible):
    rid = _make(client)
    client.post("/api/mulank/order", json={"report_id": rid})
    rz.fetch_error = RuntimeError("razorpay unreachable")
    r = client.post("/api/mulank/order", json={"report_id": rid})
    assert r.status_code == 503 and r.json()["detail"] == "order_check_failed" and len(rz.created) == 1


def test_an_order_that_is_no_longer_payable_is_replaced(client, rz, eligible):
    rid = _make(client)
    first = client.post("/api/mulank/order", json={"report_id": rid}).json()["razorpay_order_id"]
    rz.status[first] = "cancelled"
    second = client.post("/api/mulank/order", json={"report_id": rid}).json()["razorpay_order_id"]
    assert second != first and len(rz.created) == 2


def test_paid_at_razorpay_but_not_yet_recorded_is_settled_not_charged_again(client, rz, eligible, fulfilled):
    rid = _make(client)
    oid = client.post("/api/mulank/order", json={"report_id": rid}).json()["razorpay_order_id"]
    rz.status[oid] = "paid"                                          # money taken; the callback never arrived
    rz.captured[oid] = [_ent(oid)]
    r = client.post("/api/mulank/order", json={"report_id": rid})
    assert r.status_code == 200 and r.json() == {"already_paid": True}
    assert _paid(rid) and len(rz.created) == 1 and fulfilled == [rid] and _captured_rows(rid) == 1


def test_a_payment_in_flight_that_cannot_be_confirmed_yet_blocks_a_second_payment(client, rz, eligible, fulfilled):
    rid = _make(client)
    oid = client.post("/api/mulank/order", json={"report_id": rid}).json()["razorpay_order_id"]
    rz.status[oid] = "paid"
    rz.captured[oid] = [_ent(oid, amount=100)]                       # something odd: it will not verify
    r = client.post("/api/mulank/order", json={"report_id": rid})
    assert r.status_code == 409 and r.json()["detail"] == "payment_processing"
    assert not _paid(rid) and len(rz.created) == 1 and fulfilled == []


def test_a_paid_report_never_creates_another_order(client, rz, eligible, fulfilled):
    rid = _make(client)
    oid = client.post("/api/mulank/order", json={"report_id": rid}).json()["razorpay_order_id"]
    rz.entity = _ent(oid)
    sig = hmac.new(SECRET.encode(), f"{oid}|{rz.entity['id']}".encode(), hashlib.sha256).hexdigest()
    assert client.post("/api/mulank/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": rz.entity["id"],
                                                   "razorpay_signature": sig}).status_code == 200
    for _ in range(3):
        assert client.post("/api/mulank/order", json={"report_id": rid}).json() == {"already_paid": True}
    assert len(rz.created) == 1 and fulfilled == [rid] and _captured_rows(rid) == 1


def test_preorder_clicks_also_reuse_one_order_and_snapshot_the_promise_once(client, rz, preorder_on):
    rid = _make(client)                                              # nothing approved: this is a pre-order
    ids = {client.post("/api/mulank/preorder", json={"report_id": rid}).json()["razorpay_order_id"] for _ in range(3)}
    assert len(ids) == 1 and len(rz.created) == 1
    import mulank_preorder as po
    conn = db_v2.get_conn()
    try:
        snap = po.get(reports_v2.read_report(conn, rid)["payload"])
    finally:
        conn.close()
    assert snap["promise"] == "within 21 days of your order" and snap["refund_url"] == "/refund"


def test_the_buy_button_explains_a_payment_in_flight(client, rz, eligible):
    rid = _make(client)
    page = client.get(f"/en/numerology/preview/{rid}").text
    assert "We are confirming a payment for this report" in page and "Please do not pay again" in page


# ------------------------------------------------------------------ the 12-page fallback and sample content can never be delivered
def test_no_customer_facing_code_can_reach_sample_or_fallback_mode():
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for f in root.glob("*.py"):
        if f.name == "numerology_report.py":
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""mode\s*=\s*["']sample["']""", line):
                offenders.append(f"{f.name}:{n}")
    assert offenders == []


def test_a_pair_without_complete_content_gets_no_report_at_all_never_a_shorter_one(client, rz, preorder_on):
    rid = _make(client)                                              # nothing approved
    oid = client.post("/api/mulank/preorder", json={"report_id": rid}).json()["razorpay_order_id"]
    rz.entity = _ent(oid)
    sig = hmac.new(SECRET.encode(), f"{oid}|{rz.entity['id']}".encode(), hashlib.sha256).hexdigest()
    assert client.post("/api/mulank/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": rz.entity["id"],
                                                   "razorpay_signature": sig}).status_code == 200
    page = client.get(f"/en/numerology/report/{rid}")
    assert page.status_code == 200 and page.text.count('<section class="page') == 0           # not 12 cards, not 15
    assert "Pre-order received" in page.text and "not an instant download" in page.text
    assert client.get(f"/en/numerology/report/{rid}.pdf").status_code == 503
    conn = db_v2.get_conn()
    try:
        assert mf.get_state(reports_v2.read_report(conn, rid)["payload"])["status"] == mf.AWAITING
    finally:
        conn.close()


def test_the_production_report_is_always_fifteen_cards_with_no_sample_markers(monkeypatch):
    import numerology_report as R
    import mulank_qc as qc
    E = numerology.compute_mulank(29, 7, 1990, today=PIN)
    reg = {k: mc.Entry(mc.OWNER_APPROVED, editor="E", approved_by="O", approved_on="2026-11-03") for k in mc.required_keys(E)}
    r = R.build_report(29, 7, 1990, "Test Person", today=PIN, registry=reg)
    assert r["n_cards"] == 15 and r["mode"] == "production" and qc.lint_report_html(r["html"]) == []
    for other in ((15, 3, 1985), (22, 11, 2000), (9, 9, 1999)):
        with pytest.raises(R.ReportNotAvailable):
            R.build_report(*other, "Test Person", today=PIN)
