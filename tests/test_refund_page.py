"""The refund page exists, says what pre-orders promise, and is the page the pre-order flow links to."""
import re

import pytest

import mulank_preorder as po

FLAGGED = (r"anxi", r"stress", r"depress", r"panic", r"trauma", r"therap", r"counsel", r"mental", r"wellness", r"healing",
           r"diagnos", r"symptom", r"disorder", r"treatment", r"\bcure\b", r"remedy", r"disease", r"illness", r"patient",
           r"clinical", r"medic", r"doctor", r"health", r"weight")


def _text(h):
    import html
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))


def test_refund_page_is_served_and_states_the_preorder_terms(client):
    r = client.get("/refund")
    assert r.status_code == 200
    t = _text(r.text)
    for needle in ("Refund and Cancellation Policy", "A pre-order is not an instant download", "full refund",
                   "within 7 working days", "If we do not deliver on time", "support@axtroshastra.com", "never ask for your card number"):
        assert needle in t, needle
    assert 'rel="canonical" href="https://www.axtroshastra.com/refund"' in r.text


def test_refund_page_has_no_flagged_vocabulary_and_no_instant_claims(client):
    t = _text(client.get("/refund").text).lower().split("disclaimer:")[0]     # the platform footer disclaimer is injected, not part of this page
    assert not [w for w in FLAGGED if re.search(w, t)]
    assert "instant" not in t.replace("not an instant download", "")


def test_terms_cover_preorders_and_link_to_the_refund_policy(client):
    r = client.get("/terms")
    t = _text(r.text)
    assert "Pre-orders." in t and "cancel a pre-order before delivery" in t and 'href="/refund"' in r.text
    assert "A pre-order is delivered later" in t
    assert "Once a report has been generated and delivered, the purchase cannot be cancelled" in t      # existing rule unchanged


def test_the_preorder_flow_links_to_a_refund_page_that_exists(client, monkeypatch):
    monkeypatch.setenv("MULANK_PREORDER", "1")
    monkeypatch.setenv("MULANK_DELIVERY_PROMISE", "within 21 days of your order")
    monkeypatch.setenv("MULANK_REFUND_URL", "/refund")
    assert po.config()["enabled"] is True
    land = client.get("/en/numerology").text
    link = re.search(r'href="(/refund)"', land)
    assert link and client.get(link.group(1)).status_code == 200


def test_the_footer_links_do_not_break_existing_pages(client):
    for path in ("/privacy", "/terms", "/about", "/refund"):
        assert client.get(path).status_code == 200
