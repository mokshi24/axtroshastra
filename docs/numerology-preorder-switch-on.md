# Pre-order: switch-on steps (for the owner)

Pre-orders are built, tested and **off**. This kit is everything needed to turn them on safely. Nothing here changes the live site until the owner does step 3.

## What is already in place
- The pre-order flow: preview page 4 and the purchase bar say "Pre-order My Report for Rs 299", "not an instant download", the delivery time and a link to the refund policy. After paying, the customer sees "Pre-order received" and the report appears at their private link when it is ready.
- The refund and cancellation page at `/refund` and a matching pre-order paragraph in the Terms (both drafted for the owner's approval in the PR; the policy defaults are: cancel any time before delivery for a full refund, refunds within 7 working days, refund if not delivered on time, no cancellation after delivery).
- The same verified Rs 299 payment as any order (server-created order, signature, amount, currency, captured status), duplicate-safe, and Mulank-specific payment recovery.
- Ops endpoints to see and fulfil paid orders once content is approved (`docs/numerology-preorder.md`).

## What only the owner can supply
1. **Approve** the refund policy and Terms paragraph in the PR (change any default: the 7 working days, the cancellation rule).
2. **The delivery promise**: a phrase that finishes "we will deliver it ...". Set it with margin. The content for every number combination is estimated at 10 to 14 working days after the owner's content decisions, so for example `within 21 days of your order`. Missing the promise means refunds.
3. **Switch on** (after the PR is merged and deployed), on the Elastic Beanstalk environment:
   `eb setenv MULANK_PREORDER=1 MULANK_DELIVERY_PROMISE="within 21 days of your order" MULANK_REFUND_URL=/refund`
   Optional: `MULANK_LIST_PRICE_CONFIRMED=1` only if Rs 499 was a genuine regular price (shows the strike-through and the Rs 200 saving).
4. **Razorpay**: the live keys already used by the other products are used as they are. Registering `https://www.axtroshastra.com/api/mulank/webhook` (event `payment.captured`, same webhook secret) is recommended but not required: confirmed payments are also recognised when the customer returns and by the background recovery sweep.
5. **The first purchase is a test.** There is no Razorpay test mode here, so the owner (or a team member) should make the first Rs 299 pre-order themselves, check that "Pre-order received" appears and the payment shows in the dashboard, then refund it in the Razorpay dashboard.

## Switching off
`eb setenv MULANK_PREORDER=` (empty) closes pre-orders immediately. Customers who already paid keep their private link and are fulfilled as promised.

## Honouring the promise
- Content must be written and approved before the promised date (`docs/numerology-content-gap-and-plan.md`; start with the personal-year blocks).
- When content for a combination is approved, run the fulfil endpoint (`POST /api/mulank/admin/fulfil`, key in `X-Stats-Key`): every ready order's report and PDF are generated.
- **Telling customers**: customers are not messaged automatically (no WhatsApp provider or email is set up). Until the owner approves one, the pre-order page asks them to keep their link, and the team can message them manually.