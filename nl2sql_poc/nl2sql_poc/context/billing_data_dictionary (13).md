# Billing Database Context and Data Dictionary

**Purpose:** Provide the AI assistant with a reliable description of the billing database so that plain-English report requests can be mapped to the correct tables, columns, values, joins, and business definitions.

**Database:** `billing`  
**Model type:** Synthetic, simplified subscription-billing model inspired by a Stripe-style schema. The customer records and transactions in this export are generated demo data, not a live customer dataset.  
**SQL dialect:** MySQL 8.0.46 in the exported instance.  
**Snapshot:** This document's physical column types and observed values/counts are based on `Dump20261012.sql`, exported on 12 October 2026. Business-table counts and distributions can change if the synthetic sample data is regenerated; `report_views` metadata can change as reports are created.

> **Schema and value accuracy:** The column types and nullability below were extracted from the exported MySQL `CREATE TABLE` definitions. Status constraints describe values permitted by the schema; observed values/counts describe what actually occurs in this export. Treat the live schema and current data as authoritative if they differ from this snapshot.

## 1. What this database represents

This database models a software/service business that sells products through subscription plans. Customers have payment methods and subscriptions. Subscriptions generate invoices. Invoices can have payment charges, and successful charges can have refunds.

### How this context fits into the chatbot POC

The custom Power BI chatbot visual sends a user's report request to a Python Flask backend through `/chat`. The backend maintains conversation history and evaluates whether the request is sufficiently clear; if needed, it asks a focused clarification question before SQL generation. Claude then generates a MySQL `SELECT` using the live schema and this document as business context. Application guardrails validate the query before execution through the restricted read-only database connection. When the query succeeds, the backend creates a uniquely named `vw_...` database view using the separate view-builder account, records report metadata in `report_views`, and returns the view name, SQL, assumptions, and a result preview.

Creating a database view does **not** automatically build Power BI charts or dashboard visuals. That is a separate integration step. This document describes the billing data and its meaning; application code remains responsible for clarification flow, SQL validation, access controls, execution, and view creation.

- **Product:** A product/service tier that is offered to customers, such as Basic, Pro, or Business.
- **Plan:** A particular price option for a product, including its price and monthly/yearly interval.
- **Customer:** A person/customer record.
- **Payment method:** A saved card associated with a customer.
- **Subscription:** A customer's subscription to one plan, with lifecycle dates and status.
- **Invoice:** A bill for a subscription billing period.
- **Charge:** A payment attempt associated with an invoice, customer, and payment method.
- **Refund:** Money returned against a charge.

## 2. Tables and relationships

### Business tables versus technical metadata

The eight business tables are `products`, `plans`, `customers`, `payment_methods`, `subscriptions`, `invoices`, `charges`, and `refunds`. The export also contains a technical table named `report_views` (7 columns, **0 rows** in this snapshot). It stores metadata about generated reports and is not a business source for billing metrics. Do not use `report_views` as a source for business report SQL. No `report_requests` table is present in this export.


Its schema is: `view_id` INT NOT NULL AUTO_INCREMENT (primary key); `session_id` VARCHAR(64) NOT NULL; `question` TEXT NOT NULL; `sql_text` TEXT NOT NULL; `view_name` VARCHAR(128) NOT NULL (unique); `assumptions` TEXT NULL; `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP. Use it only to inspect chatbot/report metadata, not as an input for business metrics.

### Relationship map

```text
products (1) ──── (many) plans
plans (1) ─────── (many) subscriptions
customers (1) ─── (many) payment_methods
customers (1) ─── (many) subscriptions
subscriptions (1) ─── (many) invoices
invoices (1) ──── (many) charges (normally one; more can occur when a payment is retried)
payment_methods (1) ─── (many) charges
charges (1) ───── (zero or more) refunds in the relational design; the generated sample currently creates at most one refund per charge
```

### Foreign-key/join columns

| Parent | Child | Join condition | Meaning |
|---|---|---|---|
| `products` | `plans` | `products.product_id = plans.product_id` | Which product a plan belongs to |
| `plans` | `subscriptions` | `plans.plan_id = subscriptions.plan_id` | Which plan a subscription uses |
| `customers` | `payment_methods` | `customers.customer_id = payment_methods.customer_id` | Cards saved by a customer |
| `customers` | `subscriptions` | `customers.customer_id = subscriptions.customer_id` | Subscriptions owned by a customer |
| `subscriptions` | `invoices` | `subscriptions.subscription_id = invoices.subscription_id` | Invoices generated for a subscription |
| `invoices` | `charges` | `invoices.invoice_id = charges.invoice_id` | Payment attempts for an invoice |
| `payment_methods` | `charges` | `payment_methods.payment_method_id = charges.payment_method_id` | Card used for a charge |
| `charges` | `refunds` | `charges.charge_id = refunds.charge_id` | Refund(s) against a charge |

### Important join guidance

- `invoices` contains both `subscription_id` and `customer_id`; `charges` contains both `invoice_id` and `customer_id`. These are redundant routes to customer information. For consistency, prefer the path `charges → invoices → subscriptions → customers` when the relationships are present, and do not join to the same customer through both paths in one query.
- Use `charges.payment_method_id` to analyse card brand or payment method.
- When joining charges and refunds, aggregate refunds by `charge_id` before joining if the query sums charge amounts. Multiple refund rows can otherwise duplicate charge amounts and overstate revenue.
- A subscription belongs to one plan; a plan belongs to one product. Use those joins to report by plan or product.
- `charges.invoice_id` is nullable in the schema, but all 1,548 charge rows in this export have a non-NULL invoice ID.

## 3. Table: `products`

**Purpose:** Product/service catalog. One row per product offering.

| Column | MySQL type | Null? | Meaning and valid values |
|---|---|---|---|
| `product_id` | `INT` | No | Primary key identifying the product. |
| `name` | `VARCHAR(100)` | No | Unique product name. Seed values: `Basic`, `Pro`, `Business`, `Legacy Starter`. |
| `description` | `VARCHAR(255)` | Yes | Human-readable product description; may be `NULL`. |
| `active` | `TINYINT(1)` | No | Whether the product is currently offered. `1` = active/currently offered; `0` = inactive/discontinued. Do not filter inactive products out of historical reports unless requested. |

**Seed products:**

| product_id | name | description | active | Interpretation |
|---:|---|---|---|---|
| 1 | Basic | Core features for individuals | 1 | Entry-level individual offering |
| 2 | Pro | Advanced features for small teams | 1 | Advanced offering for small teams |
| 3 | Business | Full features with priority support | 1 | Business offering with priority support |
| 4 | Legacy Starter | Discontinued entry plan | 0 | Legacy product retained for historical analysis |

## 4. Table: `plans`

**Purpose:** Price list. Each plan belongs to one product and defines the billing interval and amount.

| Column | MySQL type | Null? | Meaning and valid values |
|---|---|---|---|
| `plan_id` | `INT` | No | Primary key identifying the plan. |
| `product_id` | `INT` | No | Foreign key to `products.product_id`. |
| `nickname` | `VARCHAR(120)` | No | Human-readable plan name. Seed values are listed below. |
| `billing_interval` | `VARCHAR(20)` | No | Allowed values: `month` or `year`. Describes how often the plan renews. |
| `amount_cents` | `INT` | No | Plan price in cents, not dollars. Must be non-negative. Convert to dollars by dividing by `100.0`. |
| `currency` | `CHAR(3)` | No | Currency code. Seed data uses lowercase `usd` for US dollars. |
| `active` | `TINYINT(1)` | No | `1` = plan currently sold; `0` = no longer sold. Historical subscriptions using an inactive plan must still be included in historical analysis when appropriate. |

**Seed plans and prices:**

| plan_id | product_id | nickname | billing_interval | amount_cents | Price in USD | active |
|---:|---:|---|---|---:|---:|---|
| 1 | 1 | Basic Monthly | month | 1500 | 15.00 | 1 |
| 2 | 1 | Basic Yearly | year | 15000 | 150.00 | 1 |
| 3 | 2 | Pro Monthly | month | 4900 | 49.00 | 1 |
| 4 | 2 | Pro Yearly | year | 49000 | 490.00 | 1 |
| 5 | 3 | Business Monthly | month | 14900 | 149.00 | 1 |
| 6 | 3 | Business Yearly | year | 149000 | 1490.00 | 1 |
| 7 | 4 | Starter Monthly (legacy) | month | 900 | 9.00 | 0 |

## 5. Table: `customers`

**Purpose:** Customer master records. One row per customer.

| Column | MySQL type | Null? | Meaning and valid values |
|---|---|---|---|
| `customer_id` | `VARCHAR(32)` | No | Primary key. Synthetic IDs are generated with a `cus_` prefix followed by random lowercase letters/digits. Treat as an identifier, not a measure. |
| `name` | `VARCHAR(120)` | No | Synthetic customer name. Free text. Do not infer other demographic attributes from the name. |
| `email` | `VARCHAR(255)` | No | Unique synthetic email address. Personal/contact field; do not expose it in aggregate reports unless explicitly required. |
| `country` | `CHAR(2)` | No | Two-letter country code. Generated values: `US`, `CA`, `GB`, `IN`, `AU`. See the code mapping below. |
| `created_at` | `DATETIME` | No | Timestamp when the customer record was created. Useful for customer acquisition/cohort analysis. |

**Country codes:**

| Code | Meaning | Rows in this snapshot |
|---|---|---:|
| `US` | United States | 82 |
| `CA` | Canada | 21 |
| `GB` | United Kingdom (GB is the standard country code used in this dataset) | 24 |
| `IN` | India | 11 |
| `AU` | Australia | 12 |

The dataset uses a weighted country distribution, so the sample is not intended to represent real-world country market share. Counts are a snapshot and can change after data regeneration.

## 6. Table: `payment_methods`

**Purpose:** Saved payment cards belonging to customers. A customer has one or two payment methods in this export: 118 customers have one card and 32 have two. Each of the 150 customers has exactly one default card in this snapshot.

| Column | MySQL type | Null? | Meaning and valid values |
|---|---|---|---|
| `payment_method_id` | `VARCHAR(32)` | No | Primary key. Synthetic IDs use the `pm_` prefix. |
| `customer_id` | `VARCHAR(32)` | No | Foreign key to `customers.customer_id`. The owner of the payment method. |
| `type` | `VARCHAR(30)` | No | Payment-method type. Generated value is `card`; default is `card`. |
| `card_brand` | `VARCHAR(30)` | No | Card network/brand. Values in this export: `visa` (90), `mastercard` (61), `amex` (18), `discover` (13). |
| `card_last4` | `CHAR(4)` | No | Four final card digits stored as text so leading zeros are preserved. This is not a full card number. |
| `exp_month` | `TINYINT` | No | Expiration month, integer from `1` to `12`. |
| `exp_year` | `SMALLINT` | No | Expiration year. Observed values in this export: 2025 (8 cards), 2026 (33), 2027 (38), 2028 (58), 2029 (31), 2030 (14). These counts can change when data is regenerated; do not hard-code the year list into SQL logic. |
| `is_default` | `TINYINT(1)` | No | `1` = customer's default payment method; `0` = another saved method. Each customer has exactly one default card in this export. |
| `created_at` | `DATETIME` | No | Timestamp when the payment method record was created. |

For this POC, treat a card as valid through the last day of its `exp_month`/`exp_year` unless the business owner defines a different convention. To find an expiring card, compare the expiry month/year as a date; do not compare `exp_year` alone.

## 7. Table: `subscriptions`

**Purpose:** Subscription lifecycle records. Each subscription belongs to one customer and one plan.

| Column | MySQL type | Null? | Meaning and valid values |
|---|---|---|---|
| `subscription_id` | `VARCHAR(32)` | No | Primary key. Synthetic IDs use the `sub_` prefix. |
| `customer_id` | `VARCHAR(32)` | No | Foreign key to `customers.customer_id`. Owner of the subscription. |
| `plan_id` | `INT` | No | Foreign key to `plans.plan_id`. Plan associated with the subscription. |
| `status` | `VARCHAR(20)` | No | Allowed values: `trialing`, `active`, `past_due`, `canceled`. Counts in this export: `active` (118), `canceled` (37), `past_due` (12), `trialing` (8). See meanings below. |
| `start_date` | `DATE` | No | Date when the subscription began. |
| `current_period_start` | `DATE` | No | Beginning of the current subscription/billing period. |
| `current_period_end` | `DATE` | No | End of the current subscription/billing period. |
| `cancel_at_period_end` | `TINYINT(1)` | No | `1` = true; `0` = false. In this export, the flag is `1` for 18 subscriptions and all 18 already have `status = 'canceled'`; the other 19 canceled subscriptions have `0`. It does not identify active subscriptions scheduled to cancel in this snapshot. |
| `canceled_at` | `DATETIME` | Yes | Timestamp when cancellation occurred; `NULL` for subscriptions that have no cancellation timestamp. |

**Subscription status meanings:**

- `trialing`: subscription is in a trial period. The generator creates zero-dollar invoices for trial periods.
- `active`: subscription is currently active according to the status field.
- `past_due`: payment is overdue or has not been successfully collected for the latest billing period; the generator creates an open invoice for the latest period.
- `canceled`: subscription has been canceled. Use `canceled_at` for cancellation timing when populated.

For “active subscriptions,” filter `status = 'active'` unless the business request explicitly defines a broader category that includes trialing or past-due subscriptions. Statuses represent the current state only; there is no subscription-status history in this schema.

## 8. Table: `invoices`

**Purpose:** Bills for subscription billing periods. A subscription can have multiple invoices over time.

| Column | MySQL type | Null? | Meaning and valid values |
|---|---|---|---|
| `invoice_id` | `VARCHAR(32)` | No | Primary key. Synthetic IDs use the `in_` prefix. |
| `subscription_id` | `VARCHAR(32)` | No | Foreign key to `subscriptions.subscription_id`. Subscription being billed. |
| `customer_id` | `VARCHAR(32)` | No | Foreign key to `customers.customer_id`. Customer associated with the invoice. This is also derivable through the subscription. |
| `amount_due_cents` | `INT` | No | Amount billed/due in cents; non-negative. Convert to USD by dividing by `100.0`. |
| `amount_paid_cents` | `INT` | No | Amount paid against the invoice in cents; non-negative. Convert to USD by dividing by `100.0`. |
| `currency` | `CHAR(3)` | No | Currency code. Seed data uses `usd`. |
| `status` | `VARCHAR(20)` | No | Schema-allowed values: `draft`, `open`, `paid`, `void`, `uncollectible`. Observed here: `paid` (1,490), `open` (31); there are no `draft`, `void`, or `uncollectible` rows in this export. |
| `period_start` | `DATE` | No | Start date of the service/billing period covered by this invoice. |
| `period_end` | `DATE` | No | End date of the service/billing period covered by this invoice. |
| `due_date` | `DATE` | No | Date payment is due. |
| `created_at` | `DATETIME` | No | Timestamp when the invoice record was created. |

**Invoice status meanings:**

- `draft`: invoice is a draft and is not finalized.
- `open`: invoice is outstanding/open for payment.
- `paid`: invoice is marked paid. The business rules say that for paid invoices, `amount_paid_cents = amount_due_cents`.
- `void`: invoice has been voided.
- `uncollectible`: invoice is considered uncollectible.

A trial subscription can have an invoice with both due and paid amounts equal to zero. In this export, all 52 zero-dollar invoices are marked `paid` and have no charge rows. Therefore, an invoice status of `paid` does not necessarily mean positive revenue.

## 9. Table: `charges`

**Purpose:** Individual payment attempts. An invoice normally has one charge, but a failed attempt followed by a retry can create multiple charges for the same invoice. In this export, 1,390 invoices have one charge and 79 have two. Of those 79 two-attempt invoices, 78 have a failed attempt followed by a successful attempt, and one has two failed attempts. Do not equate the number of charges with the number of invoices.

| Column | MySQL type | Null? | Meaning and valid values |
|---|---|---|---|
| `charge_id` | `VARCHAR(32)` | No | Primary key. Synthetic IDs use the `ch_` prefix. |
| `invoice_id` | `VARCHAR(32)` | Yes | Foreign key to `invoices.invoice_id`; nullable in the schema, but all 1,548 charge rows in this export have a non-NULL invoice ID. |
| `payment_method_id` | `VARCHAR(32)` | No | Foreign key to `payment_methods.payment_method_id`. Payment method used for the attempt. |
| `customer_id` | `VARCHAR(32)` | No | Foreign key to `customers.customer_id`. Customer associated with the attempt; also derivable through invoice/subscription relationships when available. |
| `amount_cents` | `INT` | No | Attempted charge amount in cents; non-negative. Convert to USD by dividing by `100.0`. |
| `currency` | `CHAR(3)` | No | Currency code. Seed data uses `usd`. |
| `status` | `VARCHAR(20)` | No | Schema-allowed values: `succeeded`, `failed`, `pending`. Observed here: `succeeded` (1,438), `failed` (110), and no `pending` rows. |
| `failure_code` | `VARCHAR(50)` | Yes | Failure reason code. Observed failed-charge codes in this export: `card_declined` (68), `insufficient_funds` (21), `expired_card` (12), `processing_error` (9). All 1,438 successful charges have `NULL` `failure_code`; no failed charge has a `NULL` failure code. |
| `created_at` | `DATETIME` | No | Timestamp of the payment attempt. Use this for charge/payment activity trends unless the requested metric needs invoice-period dates instead. |

**Charge status meanings:**

- `succeeded`: the payment attempt succeeded. Include these rows for gross collected-charge revenue.
- `failed`: the payment attempt failed. Include these rows in failed-payment analysis, not in collected revenue.
- `pending`: the payment attempt is awaiting a final outcome if present in the database. Do not count it as succeeded or failed without an explicit business rule.

**Generated failure codes:**

| Code | Meaning |
|---|---|
| `card_declined` | Issuing bank/card declined the payment |
| `insufficient_funds` | Insufficient available funds |
| `expired_card` | Card has expired |
| `processing_error` | A payment-processing error occurred |
| `NULL` | No failure code recorded; expected for successful charges in generated data |

## 10. Table: `refunds`

**Purpose:** Money returned against a charge. Refund amounts are stored in cents.

| Column | MySQL type | Null? | Meaning and valid values |
|---|---|---|---|
| `refund_id` | `VARCHAR(32)` | No | Primary key. Synthetic IDs use the `re_` prefix. |
| `charge_id` | `VARCHAR(32)` | No | Foreign key to `charges.charge_id`. Charge being refunded. |
| `amount_cents` | `INT` | No | Refund amount in cents; must be greater than zero. Convert to USD by dividing by `100.0`. In this export, no refund exceeds its charge amount; all 53 refunds are on succeeded charges, with 37 full and 16 partial refunds, and there is at most one refund per charge. |
| `reason` | `VARCHAR(50)` | No | Values in this export: `requested_by_customer` (41), `fraudulent` (7), `duplicate` (5). |
| `created_at` | `DATETIME` | No | Timestamp when the refund was recorded/created. Use this for refund-activity trends. |

**Refund reason meanings:**

- `requested_by_customer`: refund requested by the customer.
- `duplicate`: payment/refund relates to a duplicate transaction.
- `fraudulent`: transaction was considered fraudulent.

These are synthetic labels; the dataset does not include further operational evidence proving fraud.

## 11. General data and modelling rules

### Money and currency

- Fields ending in `_cents` store integer cents, not dollars.
- Convert to USD with `/ 100.0`; use clear output aliases such as `gross_revenue_usd`, `refund_amount_usd`, and `net_revenue_usd`.
- The seed plans and generated transactions use `usd` (lowercase). This is not a foreign-exchange dataset; do not apply currency conversion.
- Do not add raw cent values to dollar values.

### Revenue and payment metrics

- **Gross successful charge revenue:** sum `charges.amount_cents` where `charges.status = 'succeeded'`, converted to USD.
- **Total refunds:** sum `refunds.amount_cents`, converted to USD.
- **Net revenue after refunds:** gross successful charge revenue minus refunds. For this POC, when the user says “revenue” without specifying gross or net, use successful charges minus refunds and state that assumption. If the user explicitly asks for “gross revenue,” use successful charges before subtracting refunds. Assign charge revenue to `charges.created_at` and refunds to `refunds.created_at`.
- **Failed charge count:** count rows in `charges` where `status = 'failed'`.
- **Failed charge rate:** failed charges divided by all charge attempts. State the denominator as all recorded charge attempts, including retries; it is not the same as failed invoices divided by invoices.
- **Payment attempts versus invoices:** retries can produce multiple charge rows for one invoice. Use `COUNT(DISTINCT invoice_id)` when the business question is about invoices rather than attempts.

### Monthly recurring revenue (MRR)

For active subscriptions, use the associated plan's `amount_cents`. Monthly plans contribute the plan amount; yearly plans contribute the plan amount divided by 12. Sum across active subscriptions, convert to USD, and state this normalization as an assumption because it is a business definition adopted for this POC.

### Churn

For an overall all-time cancellation share, use canceled subscriptions divided by all subscriptions. For a period-specific churn rate, use subscriptions canceled during the period (by `canceled_at`) divided by subscriptions active at the start of the period. This is a simplified demo measure, not a complete SaaS churn model; state the definition and period used.

### Dates and time grouping

Use the date field that best matches the business event:

- `customers.created_at`: customer creation/acquisition.
- `subscriptions.start_date`: subscription starts.
- `subscriptions.canceled_at`: cancellation timing.
- `invoices.period_start` / `period_end`: service/billing period analysis.
- `invoices.due_date`: invoice due-date analysis.
- `charges.created_at`: payment-attempt and charge trends.
- `refunds.created_at`: refund activity trends.
- `payment_methods.created_at`: saved payment-method creation.

Do not silently substitute `created_at` for a billing-period date. Ask or state an assumption if the choice could materially change the report.

### Join fan-out and aggregation

- Joining `charges` to `refunds` can multiply charge rows if a charge has more than one refund. Aggregate refunds per charge before combining them with charge-level revenue.
- Joining `subscriptions` to `invoices` and then `charges` can create multiple rows per subscription. Use the correct grain for the requested metric (customer, subscription, invoice, charge, or refund).
- Count distinct identifiers when a one-to-many join would otherwise inflate counts.

### Active/inactive records

- `products.active = 0` and `plans.active = 0` mean the offering is no longer active/sold, not that its historical transactions should be deleted or excluded.
- Filter to active products/plans only if the request asks for currently active offerings or current sales catalog.
- For “currently active subscriptions,” use `subscriptions.status = 'active'` unless the user defines otherwise. `products.active` and `plans.active` are stored as `TINYINT(1)` values (`1` = active, `0` = inactive); compare with `1` or `0` in MySQL SQL.

### Unknown / nullable values

- `products.description`, `subscriptions.canceled_at`, `charges.invoice_id`, and `charges.failure_code` can be `NULL` according to the schema. In this export, `subscriptions.canceled_at` is `NULL` for all 138 non-canceled subscriptions and `charges.failure_code` is `NULL` for the 1,438 successful charges. All charges have a non-NULL `invoice_id` in this snapshot.
- Do not automatically replace `NULL` with `0` or assume it means a failed record. Interpret it according to the field meaning.
- Schema-allowed status values and values actually present in the sample differ: invoices only have `paid` and `open`; charges only have `succeeded` and `failed`. Use live distinct values for current counts.

## 12. Suggested business questions the schema can answer

- How much gross successful charge revenue and net revenue after refunds did we have by month?
- What are total successful charges, failed charges, and failed-charge rate?
- Which failure codes occur most often, and how do they trend over time?
- What is refund amount by reason, country, product, or month?
- How many active, trialing, past-due, or canceled subscriptions do we have?
- How many subscriptions and how much MRR does each plan/product contribute?
- What is revenue by product, plan, country, or billing interval?
- How many invoices are paid, open, void, uncollectible, or draft (subject to which statuses actually exist in this sample)?
- How does customer creation trend over time or by country?
- Which card brands are associated with failed charge attempts?

## 13. Sample-size snapshot

The following counts were parsed from the database export created on 12 October 2026. They are a dated snapshot, not permanent constants:

| Table | Rows in export |
|---|---:|
| `products` | 4 |
| `plans` | 7 |
| `customers` | 150 |
| `payment_methods` | 182 |
| `subscriptions` | 175 |
| `invoices` | 1,521 |
| `charges` | 1,548 |
| `refunds` | 53 |
| `report_views` (technical metadata) | 0 |

**Observed date coverage:** customers were created from 20 April 2024 to 31 August 2026; subscriptions started from 21 April 2024 to 25 September 2026; invoice periods start from 21 April 2024 to 31 October 2026; charge timestamps run from 21 April 2024 to 31 October 2026; refund timestamps run from 27 June 2024 to 15 October 2026. `DATETIME` values do not carry timezone information.

**Future-dated records in this export:** relative to its export timestamp (12 October 2026, 11:13:45), 47 invoices, 46 charges, and 1 refund have `created_at` timestamps later than the export time; 47 subscriptions have `current_period_start` after 12 October 2026. For “to date” reports, exclude future-dated activity with an appropriate `created_at <= NOW()` filter. Upcoming/scheduled billing questions are an exception. Re-check these counts if time or the dataset changes.

**Invoice/subscription relationship in this snapshot:** the 31 open invoices belong to 15 subscriptions currently marked `active`, 12 marked `past_due`, and 4 marked `canceled`. Do not equate `invoices.status = 'open'` with `subscriptions.status = 'past_due'`. The 12 current `past_due` subscriptions each have an open invoice in this snapshot.

**Cross-table consistency checks on this export:** invoice customer IDs match the customer reached through the subscription; charge customer IDs match both the invoice's customer and the payment method's owner; every charge amount equals the invoice amount due; paid invoices have `amount_paid_cents = amount_due_cents`; open invoices have `amount_paid_cents = 0`; and each invoice's `due_date` is `period_start + 3 days`. These checks passed on this export and should be re-run if data-generation logic changes.

The dataset is generated synthetic data. Do not infer real market trends or customer behaviour from its generated weighted distributions.

## 14. Instructions for the analytics assistant

When a user submits a plain-English report request:

1. Identify the business metric and desired grain (customer, subscription, invoice, charge, refund, or time period).
2. Map business terms to the tables and columns defined here; never invent fields or status values.
3. Check whether the requested data exists in the live schema.
4. If an important choice is genuinely ambiguous (for example, gross versus net revenue, or invoice period versus charge date), ask one concise clarification question or state an explicit assumption according to the application's prompt-evaluation policy.
5. Generate MySQL SQL using only the tables allowed by configuration. The application, not the model, must validate SQL before executing it.
6. Convert cents to USD and make units explicit in result aliases.
7. Use joins and aggregation at the correct grain; avoid duplicating charges or customers through one-to-many joins.
8. When a view is created for a report, give it a clear, unique name and retain the originating question and assumptions as metadata.

---

*This context was cross-checked against `Dump20261012.sql` for physical column types, nullability, row counts, distinct values, and selected relationship/business-rule consistency checks. Re-audit against the live database if the schema or generated data changes.*
