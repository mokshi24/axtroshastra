# Mulank report delivery after payment: how it works

One existing system, no second one: the five-page preview, the server-verified Rs 299 Razorpay payment, and the approved 15-page report renderer.

## Journey
1. Customer enters details on `/en/numerology` (name, date of birth, gender, WhatsApp number).
2. They get the five-page free preview (page, PDF).
3. Combination ready (content approved and complete): the purchase card after page 3 says "Unlock My Full Numerology Report - Rs 299".
   Combination not ready and pre-orders open: the card says "Pre-order My Report for Rs 299", shows the owner-approved delivery time and the refund link, and "This is a pre-order, not an instant download". Pre-orders closed: an availability message and no payment button.
4. Razorpay checkout: the order is created on the server at exactly 29,900 paise INR; the browser never sets a price.
5. After payment the server verifies signature, order, amount, currency and captured status. Only then:
   - ready: the private report page shows the 15-page report and a "Download PDF" button;
   - pre-order: "Pre-order received... not an instant download... delivered <promise>", no report and no PDF yet.
6. The same private link works again and again, with no second payment. When a pre-ordered report is ready (content approved, then `POST /api/mulank/admin/fulfil`), the same link shows the report and PDF.

## Guarantees and where they are enforced
| Requirement | How |
|---|---|
| Use the approved 15-page report; keep design and personalisation | `numerology_report.py` is the only renderer; text matches the approved v4 sample on all 15 pages (tested) |
| Never the 12-page fallback, never sample content | production mode renders only when every part is approved and complete, otherwise it raises: no shorter report exists; sample mode is unreachable from customer code (tested by a source scan) and refused with live keys; a last guard refuses any report with a fallback or sample marker |
| Pre-order stated before payment | preview page 4, the purchase bar and the Razorpay description; the exact promise and refund link shown are stored on the report |
| Pre-order confirmation, not an instant download | the "Pre-order received" page; PDF returns 503 until the report exists |
| Do not charge twice | one payable order per report: a repeat click, a second tab or a retry reuses the open order; if Razorpay already shows it paid, it is recorded (not paid again); if Razorpay cannot be asked, no second order is created; a paid report never creates another order; duplicate callbacks fulfil once |
| Secure access | private random link; only for a paid report; unpaid or unknown links look identical (404); no-store, noindex |
| Download again without paying | `already_paid`; cached PDF |

## Still open (not code)
- Content for most number combinations (`docs/numerology-content-gap-and-plan.md`): until approved, only the pre-order is possible, and only once the owner opens it (`docs/numerology-preorder-switch-on.md`).
- Telling customers when a pre-ordered report is ready: no WhatsApp provider or email is configured; customers are asked to keep their link.
- Refunds are made by the owner in the Razorpay dashboard.