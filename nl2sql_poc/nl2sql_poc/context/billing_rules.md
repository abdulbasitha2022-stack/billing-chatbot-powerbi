# Billing schema - business rules

A simplified Stripe-style billing model: customers, subscriptions, invoices, charges, refunds.

## Tables
- `products` / `plans` - the price list. `plans.billing_interval` is 'month' or 'year'.
  `active = false` means the plan is no longer sold (don't exclude it from historical queries).
- `customers` - one row per customer.
- `payment_methods` - cards on file. `is_default = true` marks the default card.
- `subscriptions` - `status` is one of trialing / active / past_due / canceled.
- `invoices` - one per billing period. `status` is draft / open / paid / void / uncollectible.
  A 'paid' invoice always has `amount_paid_cents = amount_due_cents`.
- `charges` - normally one per invoice; two when a first attempt failed and was retried.
  `status` is succeeded / failed / pending.
- `refunds` - against a charge. Never exceeds that charge's amount.

## Units
All money columns are **cents**, integers. Divide by 100.0 for a dollar amount. Always alias
the result with "_usd" or similar so the unit is clear in the output.

## Relationship paths
`charges` and `invoices` each have a `customer_id` column AND reach the customer indirectly
(charges -> invoices -> subscriptions -> customers). Prefer the indirect path through
`subscriptions` for consistency; don't join on both paths at once in the same query.

## Common metrics
- Revenue: SUM(charges.amount_cents) WHERE charges.status = 'succeeded', minus refunds.
- MRR (Monthly Recurring Revenue): for active subscriptions, the plan amount, divided by 12
  for yearly plans, summed across all active subscriptions. State this normalization in an
  ASSUMPTION line - it's a judgment call, not a documented fixed rule.
- Churn: canceled subscriptions as a fraction of all subscriptions (state the time window used).
- Failed charge rate: failed charges as a fraction of all charges.
