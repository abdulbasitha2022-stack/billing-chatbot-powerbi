# Billing POC — Compact Runtime Context for SQL Generation

This file is a token-efficient runtime summary for the billing-report chatbot. The full `billing_data_dictionary.md` remains the detailed reference document. The live schema is supplied separately by the application, so this file focuses on business meaning and query rules.

## Project and database

The POC accepts English report requests and generates read-only MySQL queries for a synthetic subscription-billing database. The business tables are `products`, `plans`, `customers`, `payment_methods`, `subscriptions`, `invoices`, `charges`, and `refunds`. Do not use system bookkeeping tables such as `report_views`, `report_request_metadata`, or `report_requests` as business sources; do not use generated views whose names start with `vw_` as source tables unless explicitly requested.

## Relationships

- `plans.product_id` → `products.product_id`
- `payment_methods.customer_id` → `customers.customer_id`
- `subscriptions.customer_id` → `customers.customer_id`; `subscriptions.plan_id` → `plans.plan_id`
- `invoices.subscription_id` → `subscriptions.subscription_id`; `invoices.customer_id` → `customers.customer_id`
- `charges.invoice_id` → `invoices.invoice_id`; `charges.payment_method_id` → `payment_methods.payment_method_id`; `charges.customer_id` → `customers.customer_id`
- `refunds.charge_id` → `charges.charge_id`

The `customer_id` stored on invoices and charges is a valid direct join to `customers`. Avoid joining to the same customer through multiple paths in one query, which can duplicate rows. Join charges to refunds at charge level and aggregate carefully so refund joins do not multiply charge amounts.

## Units and key meanings

- Money fields ending `_cents` are integer US cents; convert to dollars with `/ 100.0` and use aliases ending `_usd`. Currency in this dataset is `usd` only.
- Boolean-like fields use `1` = true/yes and `0` = false/no.
- `products.active` and `plans.active` indicate whether an item is currently sold. Keep inactive plans/products in historical reports unless the request asks only for currently offered plans.
- Subscription statuses: `active`, `trialing`, `past_due`, `canceled`. `trialing` subscriptions have $0 invoices and no charges. Status is current state only; there is no status history.
- Invoice statuses observed: `paid`, `open`. A paid invoice can be a $0 trial invoice, so a paid invoice does not prove revenue was collected.
- Charge statuses: `succeeded`, `failed` (schema also allows `pending`). Failed charge rows are payment attempts, not necessarily distinct invoices; retries can create multiple charge rows for one invoice.
- Successful charges normally have `failure_code = NULL`. Failure codes may include `card_declined`, `insufficient_funds`, `expired_card`, and `processing_error`.
- Refund reasons include `requested_by_customer`, `duplicate`, and `fraudulent`. A refund is linked to a successful charge and is at most one refund per charge in this sample.
- `payment_methods.is_default = 1` identifies a customer's default card. Cards remain valid through the last day of their expiry month.

## Default metric definitions

Apply these definitions when the request does not specify another definition, and state the assumption in the SQL response where useful:

- **Gross revenue / revenue / sales / collected:** sum `charges.amount_cents` where `charges.status = 'succeeded'`, divided by 100. Do not subtract refunds from gross revenue.
- **Refunds:** sum `refunds.amount_cents`, divided by 100; use `refunds.created_at` for when the refund occurred.
- **Net revenue:** successful charge amounts minus refund amounts. For period reporting, date each component by its own event timestamp.
- **Billed/invoiced:** sum `invoices.amount_due_cents`, whether paid or open.
- **Outstanding/unpaid:** sum `amount_due_cents - amount_paid_cents` for `invoices.status = 'open'`.
- **Overdue invoice:** open invoice where `due_date < CURDATE()`.
- **Active subscription:** `subscriptions.status = 'active'`; do not include trialing, past_due, or canceled unless requested.
- **Active customer:** customer with at least one active subscription; count distinct customer IDs.
- **Paying customer in a period:** customer with at least one successful charge in that period; count distinct customer IDs.
- **MRR:** sum the plan price for active subscriptions, normalizing yearly plan price by 12. Exclude trialing, past_due, and canceled subscriptions.
- **ARR:** MRR multiplied by 12. **ARPU:** MRR divided by the number of distinct active customers.
- **Churned subscription:** canceled subscription, dated by `canceled_at`. For period churn rate, divide subscriptions canceled in the period by subscriptions active at the beginning of that period; state the definition used.
- **Failed payment rate:** failed charge attempts divided by all charge attempts (per attempt), not per invoice.
- **Retry recovery rate:** invoices with a failed attempt followed by a later successful attempt divided by invoices with any failed attempt.
- **Refund rate:** refunded dollars divided by gross successful-charge revenue for the same period; state the date basis.
- **Upcoming renewals:** active subscriptions with `current_period_end` in the requested future window.

## Data quirks and limits

- Sample data can contain future-dated invoices/charges and future billing periods due to how the synthetic data was generated. For reports covering activity up to the present, include `created_at <= NOW()` on event tables, unless upcoming/scheduled billing is explicitly requested. Apply the equivalent date filter to refunds when reporting refunds to date.
- An open invoice does not necessarily mean its subscription is `past_due`; use invoice status for unpaid invoices and subscription status for past-due subscriptions.
- `charges.failure_code = 'expired_card'` is independent of the actual expiry date stored on the payment method.
- The database has no marketing/acquisition data, product usage, cancellation reasons, discounts/coupons/tax, costs/profit, plan-change history, customer industry, support tickets, multiple currencies, or historical status snapshots. Explain the limitation rather than inventing data.

## MySQL and output rules

- Generate one read-only MySQL `SELECT` (CTEs allowed); never invent tables, columns, or categorical values.
- Use only the live schema supplied by the application. Never use `SELECT *`. Give every output field a unique readable `snake_case` alias.
- For dashboard time series, return a true `DATE` column representing the first day of each month, not a formatted text month. For example: `DATE_SUB(DATE(created_at), INTERVAL DAYOFMONTH(created_at) - 1 DAY) AS month_start`.
- For “last 12 months” when interpreted as 12 calendar months including the current month, filter from the first day of the month 11 months before the current month and through `NOW()`, excluding future-dated rows. State this interpretation as an assumption.
- MySQL 8 supports CTEs and window functions. Use `DATE_FORMAT` only when a text label is explicitly needed; use `LIMIT` for row caps. No `DATE_TRUNC`, `FILTER (WHERE ...)`, `::` casts, or `FULL OUTER JOIN`.
- Ask one focused clarification only when a material business decision cannot be resolved with the defaults above. Do not ask whether gross revenue should have refunds subtracted: gross revenue is before refunds; net revenue subtracts refunds.
