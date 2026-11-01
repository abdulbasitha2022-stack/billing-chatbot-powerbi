# Data Dictionary — `membership_sample_1000.sql`

Output columns in SELECT order. 81 columns, one row per `(ui, member_number, master_associate)`.

**Source aliases**

| Alias | Table | Join key |
|---|---|---|
| `b` | `discovery.member_info_1` | base |
| `c` | `discovery.customer_1` | `member_number` + `master_associate` |
| `ab` | `discovery.autobill_information` | `membership_number` (household) |
| `cc` | `discovery03.credit_card_information_1` | `cc_surrogate_key` (household) |
| `pd` | `discovery.payment_detail` | `member_number` + `master_associate` |
| `bpay` | `discovery.batch_payments` | `membership_number` (household) |
| `ec` | `discovery07.member_expiration_date_changes` | `member_number` + `master_associate` |
| `br` | `discovery07.ref_member_branches` | `member_branch_number` |

**Tags**

| Tag | Meaning |
|---|---|
| **[SYNTHETIC]** | Generated, not sourced. Deterministic per member |
| **[CODED]** | Values are codes. Domain in the Code Reference below |
| **[FLAG]** | Binary. The encoding is not consistent across columns — see the Code Reference |

---
## Identity

| Column | Source | Type | Description |
|---|---|---|---|
| `ui` | `b` | STRING | 10-digit System A person identifier. Person-level key. `0` excluded in `base`. |
| `member_number` | `b` | STRING | 8-digit household membership number. Shared across the household. |
| `master_associate` | `b` | STRING | Household position. `0` = primary, `1`+ = associates. AutoBill charges only `0`. **[CODED]** |
| `first_name` | `c` | STRING | Given name. **[SYNTHETIC]** |
| `middle_name` | `c` | STRING | Middle name. **[SYNTHETIC]** |
| `last_name` | `c` | STRING | Family name. **[SYNTHETIC]** |
| `member_type` | `b` | STRING | Membership type code. Decode via `ref_member_type_2`. **[CODED]** |
| `member_relationship_code` | `b` | STRING | `P` Primary, `S` Spouse, `C` Child, `O` Other, `U` Unknown. `U` appears on primaries in historical records — key primary status off `master_associate`, not this. **[CODED]** |
| `associate_count` | `b` | FLOAT64 | Number of associates on the household. |

## Tier and attached products

Each priced product is a code plus a start date, stop date and cancel indicator.

| Column | Source | Type | Description |
|---|---|---|---|
| `plus_code` | `b` | STRING | Plus tier held. **[FLAG]** |
| `plus_start_date` | `b` | TIMESTAMP | Plus start. |
| `plus_stop_date` | `b` | TIMESTAMP | Plus end. Past date with an active membership indicates a downgrade. |
| `plus_cancel_ind` | `b` | STRING | Plus cancellation pending. **[CODED]** |
| `premier_code` | `b` | STRING | Premier tier held. Outranks Plus. **[FLAG]** |
| `premier_start_date` | `b` | TIMESTAMP | Premier start. Later than `plus_start_date` indicates an upgrade. |
| `premier_stop_date` | `b` | TIMESTAMP | Premier end. |
| `premier_cancel_ind` | `b` | STRING | Premier cancellation pending. **[CODED]** |
| `rv_code` | `b` | STRING | RV rider attached. **[CODED]** |
| `rv_start_date` | `b` | TIMESTAMP | RV rider start. |
| `rv_stop_date` | `b` | TIMESTAMP | RV rider end. |
| `rv_cancel_ind` | `b` | STRING | RV rider cancellation pending. **[CODED]** |
| `motorcycle_code` | `b` | STRING | Motorcycle rider attached. **[FLAG]** |
| `motorcycle_start_date` | `b` | TIMESTAMP | Motorcycle rider start. |
| `motorcycle_stop_date` | `b` | TIMESTAMP | Motorcycle rider end. |
| `mpp_cancelled_flag` | `b` | STRING | Membership protection plan cancelled. **[FLAG]** |
| `ers_vip_member` | `b` | STRING | VIP roadside entitlement. Service-level, not a priced product. **[FLAG]** |
| `aaa_visa` | `b` | STRING | Holds an AAA-branded Visa. Correlates with the lower-decline `aaa_*` batch streams. **[FLAG]** |

There is no single tier column. Collapsing these into one label requires an explicit precedence rule (Premier > Plus > Classic) — a business decision, not a data fact.

## Dates and tenure

| Column | Source | Type | Description |
|---|---|---|---|
| `added_ts` | `b` | TIMESTAMP | Row creation. Also the sort key for "latest 1,000". |
| `expiration_date` | `b` | TIMESTAMP | Current membership expiry. Anchor for all renewal timing. |
| `extra_months_added` | `b` | INT64 | Months granted beyond the standard term. Subtract before reading an expiry advance as a normal renewal. |
| `year_joined_aaa` | `b` | FLOAT64 | Year of first AAA join. |
| `year_joined_acs` | `b` | FLOAT64 | Year of join on ACS. May differ from the above for migrated members. |
| `classic_effective_date` | `b` | TIMESTAMP | When the current Classic term took effect. |
| `payment_date` | `b` | TIMESTAMP | Most recent payment at membership level. Coarser than `pd.payment_ts`. |

A member may hold multiple `member_number` values over their life. True original join date requires the earliest join across all `member_info_1` rows for a `ui` — a single row understates tenure for anyone who changed membership number.

## Billing

| Column | Source | Type | Description |
|---|---|---|---|
| `autobill_flag` | `b` | STRING | `Y`/`N`. Member-level AutoBill enrolment. Not sufficient alone. **[CODED]** |
| `do_not_bill` | `b` | STRING | `0` = billable. Non-zero = excluded from billing entirely, never reaches the CC batch. **[CODED]** |
| `next_bill_action` | `b` | STRING | `0` = standard renewal. Non-zero = special handling. **[CODED]** |
| `billing_plan` | `b` | STRING | Current plan code. Encodes region and programme type. **[CODED]** |
| `succeeding_billing_plan` | `b` | STRING | Plan after the current term. Differing from `billing_plan` means the next bill changes. **[CODED]** |
| `amount_billed` | `b` | FLOAT64 | Billed for the current term. |
| `amount_paid` | `b` | FLOAT64 | Collected for the current term. |
| `how_joined` | `b` | STRING | Join channel. **[CODED]** |

## AutoBill — household level

These repeat across every member of a household. Deduplicate to `master_associate = '0'` before counting or summing.

| Column | Source | Type | Description |
|---|---|---|---|
| `autobill_added_ts` | `ab` | TIMESTAMP | When AutoBill enrolment was created. |
| `autobill_modified_ts` | `ab` | TIMESTAMP | Last change to enrolment. |
| `autobill_modified_by` | `ab` | STRING | Who last changed it. `DSABREJS2` is the automated disenrolment process triggered after repeated declines — the involuntary drop-off signal. **[CODED]** |

**Enrolment is the conjunction of three things:** `autobill_flag = 'Y'`, `do_not_bill = '0'`, and a non-sentinel instrument key. Any one alone misclassifies.

## Billed vs paid — latest cycle

Per member. One row only; prior cycles are not returned.

| Column | Source | Type | Description |
|---|---|---|---|
| `billed_total` | `pd` | FLOAT64 | Total billed this cycle. |
| `paid_total` | `pd` | FLOAT64 | Total collected this cycle. The difference against `billed_total` is the outstanding balance. |
| `billed_date` | `pd` | TIMESTAMP | When the bill was raised. |
| `billed_expiration_date` | `pd` | TIMESTAMP | Expiry the bill was intended to purchase. |
| `payment_ts` | `pd` | TIMESTAMP | When payment landed. **Null against a populated `billed_date` means nothing was ever collected for that cycle.** |
| `payment_type` | `pd` | STRING | Payment classification for this cycle. Carries the same values as `membership_payment_category`. **[CODED]** |
| `membership_payment_category` | `pd` | STRING | Payment classification. Decode via `ref_payment_categories`. Separates new join from renewal from reinstatement. **[CODED]** |
| `membership_payment_subcategory` | `pd` | STRING | Sub-classification. Decode via `ref_payment_subcategories`. **[CODED]** |
| `tenure` | `pd` | INT64 | Pre-calculated tenure in years, stored in the source. |
| `payment_batch_number` | `pd` | STRING | Links to `batch_payments` and `payment_transaction_master`. |

Latest cycle is selected by `COALESCE(payment_ts, billed_date) DESC`. The fallback matters — an unpaid cycle has a null `payment_ts` and would otherwise sort last, hiding exactly the rows of interest.

## Scheduled charge — latest cycle, household level

Repeats across associates.

| Column | Source | Type | Description |
|---|---|---|---|
| `scheduled_amount` | `bpay` | FLOAT64 | Amount scheduled to be charged. Net of loyalty is `scheduled_amount - aaadollars_redeemed`. |
| `scheduled_process_date` | `bpay` | TIMESTAMP | When the charge was scheduled — effectively the due date. |
| `process_status` | `bpay` | STRING | `P` queued, `F` failed, `H` on hold, `C` sent to gateway. **`C` means sent, not approved** — card approval is in `cc_batch_detail`. **[CODED]** |
| `primary_ui` | `bpay` | STRING | 10-digit System A id of the household's primary member. The confirmed System A ↔ System B identity bridge — the only place both identifiers sit on one row. |
| `batch_payment_type` | `bpay` | INT64 | Channel: `9` non-AAA CC AutoBill, `5` AAA CC AutoBill, `3` EFT, `11`/`12` lockbox check, `13` homebanking, `1` remittance. Decode via `ref_batch_payment_types`. **[CODED]** |
| `batch_payment_batch_number` | `bpay` | STRING | Batch run identifier. Links to `payment_detail.payment_batch_number` and `payment_transaction_master`. |
| `aaadollars_redeemed` | `bpay` | FLOAT64 | Loyalty dollars applied to this charge. Net expected charge is `scheduled_amount - aaadollars_redeemed`. Usually zero. |
| `batch_added_ts` | `bpay` | TIMESTAMP | Batch load timestamp. |
| `batch_added_by` | `bpay` | STRING | Source process that loaded the row. Identifies which batch stream queued the member. **[CODED]** |
| `batch_modified_ts` | `bpay` | TIMESTAMP | Last update — typically when `process_status` was written after the run. |
| `batch_modified_by` | `bpay` | STRING | Who last updated the row. `DB2BATCH` is the CC AutoBill batch processor. **[CODED]** |

## Card on file — household level

Reached via `ab.cc_surrogate_key`, not by `ui`. Roughly 21% of UIs hold multiple distinct cards, so a UI join returns an arbitrary one; the surrogate key returns the card AutoBill will actually charge.

| Column | Source | Type | Description |
|---|---|---|---|
| `cc_type` | `cc` | STRING | Card network code. Trimmed on read; source carries trailing spaces. **[CODED]** |
| `cc_last_4` | `cc` | STRING | Last four digits. Safe for member-facing confirmation. **[SYNTHETIC]** |
| `card_holder_name` | `cc` | STRING | Name on the card. May differ from the member name where the card is a spouse's. **[SYNTHETIC]** |
| `card_modified_ts` | `cc` | TIMESTAMP | When the card record was last updated. Best proxy for "when did they last touch their card". |
| `card_expiration_year` | `b` | FLOAT64 | Expiry **year only**. No month exists on the vault table. Compare against the current year for expired-card risk. |

## Last renewal event

| Column | Source | Type | Description |
|---|---|---|---|
| `old_expiration_date` | `ec` | DATE | Expiry before the most recent change. |
| `new_expiration_date` | `ec` | DATE | Expiry after. |
| `exp_change_ts` | `ec` | TIMESTAMP | When the change was written. A large gap from `old_expiration_date` means the member sat in grace before the renewal processed. |

The months between `old_expiration_date` and `new_expiration_date` are the renewal signal: ~12 = standard annual renewal, 15–20 = grace or reinstatement. Both columns null = no expiry change on record.

`member_expiration_date_changes` has no `added_by`, so it cannot say who made the change or through which channel. Cross-reference `payment_transaction_master` for that.

## Status and geography

| Column | Source | Type | Description |
|---|---|---|---|
| `inactive_flag` | `b` | STRING | `0` = active. Filtered to `0` in `base`. **[CODED]** |
| `member_branch_num` | `b` | FLOAT64 | Branch of record. Two codes are known not to resolve — a reference-data gap, not a defect. **[CODED]** |
| `member_branch_name` | `br` | STRING | Branch name across 83 branches. |
| `address_1` | `c` | STRING | Address line 1. **[SYNTHETIC]** |
| `address_2` | `c` | STRING | Address line 2. **[SYNTHETIC]** |
| `city` | `c` | STRING | City. **[SYNTHETIC]** |
| `modified_ts` | `b` | TIMESTAMP | Last update to the member row. |

---

---

# Code Reference

Value domains for every coded and flag column.

### `membership_payment_category` — why this payment happened

Single character. Full taxonomy, confirmed in production:

| Code | Meaning | Notes |
|---|---|---|
| `N` | New | First-time join |
| `R` | Renewals | Standard annual renewal — the main population |
| `S` | Reinstates | Lapsed member reinstating. **The win-back signal.** |
| `U` | Upgrades | Tier upgrade |
| `I` | Part Pays | Installment plan. `billed_total` exceeds `paid_total` **by design** — not a failure |
| `P` | Pre Pays | Paying ahead for a future renewal period |
| `T` | Transfer | Moving between AAA club regions |
| `C` | Courtesy Transfer | Transferred as a courtesy — no payment expected |
| `D` | Dues Deferral Member Cancel | Dues deferred, then cancelled |
| `O` | Other | Catch-all |
| `Z` | Unspecified | Legacy, never classified. Common — treat as unknown rather than as a category |

For renewal analysis filter to `R`; add `S` to include win-backs. `Z` is large enough that excluding it silently will skew any rate you calculate.


### `membership_payment_subcategory` — how it was collected

Numeric. **The reference table is incomplete** — only `999` is formally documented. Everything else below was confirmed from production data, not from `ref_payment_subcategories`. This is a known catalog gap.

| Code | Parent category | Meaning |
|---|---|---|
| `2` | `R` | AutoBill standard CC renewal. 20M+ rows — the core AutoBill population, routes to `cc_batch_detail` |
| `5` | `R` | AutoBill AAA-branded card renewal. Routes to `aaa_cc_batch_detail` |
| `8` | `N` | New member joining |
| `21` | `S` | Lapsed member reinstating |
| `58` | `R` | Rewards-based renewal — AAA Dollars applied |
| `69` | `I` | Installment plan renewal |
| `81` | `I` | Georgia installment plan (`SDGA501`) |
| `82` | `I` | Florida installment plan (`SDFL501`) |
| `999` | `Z` | Legacy unclassified — the only code in the reference table |

Codes outside this list exist in the data. Do not assume the list is exhaustive.


### `how_joined` — acquisition channel at join

| Code | Meaning |
|---|---|
| `99` | Phone or online |
| `DM` | Direct mail |
| `DS` | Unknown |
| blank | Not recorded |

No decode table exists for this column. Codes other than `99` are unconfirmed.


### `member_type` — membership lifecycle type

Single character. Six values, the complete domain. Decodes via `ref_member_type_2`.

| Code | Description | Meaning |
|---|---|---|
| `N` | New | First-time member joining AAA |
| `R` | Renewal | Existing member renewing annual dues. **Dominant value in `member_info_1`** |
| `T` | Transfer | Moving between AAA club regions. Creates a new `member_number`, but `ui` stays the same |
| `C` | Courtesy Transfer | Transferred as a courtesy — no payment collected at transfer time |
| `E` | "AAA" | Description in the reference table is literally "AAA". Meaning unclear; 332 rows in `member_application`, tagged "Extension" in one place. Flagged in the document as needing SME clarification |
| (blank) | "AAA" | Stored as a space. Also described only as "AAA". The document calls this a **reference-table data-quality issue** — blank should not be a valid code |

**Decode join:** `ref_member_type_2`, on `member_type_code`. Description is `member_type_desc`.

**`R` does not mean a renewal happened.** In `member_info_1` it reflects current lifecycle *state* and is dominant. In `member_application` it is effectively absent (319 rows, 0.003%) because renewals do not write application rows. Renewal *events* come from advancing expiry in `member_expiration_date_changes` and from the payment tables — not from this column.

Two of six codes (`E` and blank) have uninformative descriptions, so roughly a third of the domain is not genuinely decoded.


### `member_relationship_code` — household position

| Code | Meaning |
|---|---|
| `P` | Primary |
| `S` | Spouse |
| `C` | Child |
| `O` | Other |
| `U` | Unknown |

`U` appears on primaries in historical records, so key primary status off `master_associate = '0'`, never off this column.


### `payment_type` (from `payment_detail`) — payment channel

Note this is a **different domain** from `batch_payment_type` below. Paired with a sub-type for card network:

| `payment_type` | Meaning |
|---|---|
| `3` | Card payment — sub-type gives the network: `V` Visa, `M` Mastercard, `E` EFT/ACH, `D` Discover, `A` Amex |
| `1` | Non-card — remittance, cash or cheque |
| `5` | Separate channel, 675K rows |
| NULL | Not recorded — 1.4M rows |


### `batch_payment_type` (from `batch_payments`) — batch channel

INT64. Decodes via `ref_batch_payment_types`. All 15 confirmed:

| Code | Meaning |
|---|---|
| `9` | Non-AAA credit cards — standard CC AutoBill. **The core decline population**, routes to `cc_batch_detail` |
| `5` | AAA-branded credit cards. Routes to `aaa_cc_batch_detail`, decline rate 10–13% versus ~22% |
| `3` | Truist — EFT/ACH bank debit (was Suntrust, renamed 2024) |
| `1` | Remittance — mail-in |
| `2` | Installments |
| `4` | PPS payment |
| `10` | Lockbox credit |
| `11` | Lockbox check |
| `12` | Lockbox — generic NR |
| `13` | Homebanking |
| `14` | Affinity |
| `15` | Non-affinity |
| `16` | ACH group |
| `17` | US Bank affinity |
| `18` | Carolina payment (added Mar 2024) |

`IN (9, 5)` isolates the CC AutoBill population.


### `process_status` — batch outcome

| Code | Meaning |
|---|---|
| `C` | Completed — **sent to the gateway successfully. Not card approval.** Card outcome is in `cc_batch_detail.response_code` |
| `F` | Failed during processing — before or during gateway submission |
| `H` | On hold for manual back-office resolution. Some rows held since 2009 |
| `P` | Queued, awaiting pickup |

`ref_batch_process_status` contains only `C`, so joining to it for a decode drops the other three values.


### `cc_type` — card network

| Code | Network |
|---|---|
| `VI` | Visa |
| `MC` | Mastercard |
| `AE` | Amex |
| `DS` | Discover |
| `RV` | AAA-branded Visa |
| `RM` | AAA-branded Mastercard |

Source carries trailing spaces; the query trims on read.


### Flag columns

| Column | Domain |
|---|---|
| `autobill_flag` | `Y` / `N` |
| `do_not_bill` | `0` = billable. Non-zero = excluded from billing entirely, invisible to the CC batch. `8` is a confirmed exclusion type |
| `next_bill_action` | `0` = standard renewal. Non-zero = special handling |
| `inactive_flag` | `0` = active. Filtered to `0` in `base` |
| `master_associate` | `0` = primary, `1`/`2`/`3` = associate positions |


### `billing_plan` and `succeeding_billing_plan` — plan code

Not an opaque code — the string itself encodes three components:

```
SDFL201
│││  │
││└──┘── Sequence / tier number (201 = Tier 1)
│└────── State code (FL Florida, GA Georgia, TN Tennessee, MI Michigan, ...)
└─────── Programme prefix (SD Sliding Dues, STD Standard, RET Retail,
         SAV Savings/Financial Assistance, WFEE Waived Fee, ...)
```

| Plan ID | Decoded |
|---|---|
| `SDFL201` | Sliding Dues — Florida — Tier 1 |
| `SDGA201` | Sliding Dues — Georgia — Tier 1 |
| `SDTN201` | Sliding Dues — Tennessee — Tier 1 |
| `STDPR01` | Standard Premier — Tier 1 |
| `RETMBRT` | Retail Membership — standard full-year retail |
| `SAVFANF` | Savings / Financial Assistance — Florida |
| `WFEEFL` | Waived Fee — Florida |
| `KOWANMI` | Kowalski — Michigan (specific programme) |

Every state has its own version of a plan because prices differ by region. The prefix distinguishes the pricing model. `succeeding_billing_plan` differing from `billing_plan` means the member's next bill changes — a prefix change (e.g. `SD` to `RET`) is a bigger signal than a tier change.


### `added_by` / `modified_by` columns — actor codes

Applies to `autobill_modified_by`, `batch_added_by`, `batch_modified_by`. Two shapes:

| Pattern | Meaning |
|---|---|
| `DB2BATCH` | DB2 system batch processor |
| `DSCONVLD` | Data conversion load |
| `DSABREJS2` | **Automated AutoBill disenrolment** after repeated declines. The involuntary drop-off signal on `autobill_modified_by` |
| `Dsab9` | Non-AAA CC AutoBill batch load |
| `Dsab5` | AAA CC AutoBill batch load |
| `Dssunt` | Truist EFT batch |
| `Dsbatpock` | Lockbox batch |
| `Dsbatnrpo` | NR batch |
| `100008262` | **Customer self-service via the dot-com online channel** — confirmed by SME. Any row with this actor was member-initiated online |
| Other 9-digit numerics | Individual agent security IDs — branch or call-centre staff |

The distinction that matters: an alphabetic code means a system process acted; a 9-digit number means a human did, and `100008262` specifically means the member did it themselves.


### `*_cancel_ind` — explicit cancellation indicators

Applies to `plus_cancel_ind`, `premier_cancel_ind`, `rv_cancel_ind`.

| Value | Meaning |
|---|---|
| `Y` | Explicitly cancelled by member request — a deliberate downgrade |
| null / blank | Not explicitly cancelled |

The distinction from `stop_date` matters: a stop date alone can mean the term simply expired, whereas `Y` means the member actively asked to drop the product. Rarely populated.


### `member_branch_num` — branch of record

Numeric code, decoded to `member_branch_name` by the `ref_member_branches` join already in the query. 83 branches. Two codes are known not to resolve — a reference-data gap, not a query defect, so expect a small number of null `member_branch_name` values.


### Product columns

Encoding is not consistent across products — some are numeric, some alphabetic, and `rv_code` is not binary at all. A predicate written for one will not work on another.

| Column | Values | Meaning |
|---|---|---|
| `plus_code` | `0` / `1` | `1` = Plus tier held |
| `premier_code` | `N` / `Y` | `Y` = Premier tier held |
| `motorcycle_code` | `N` / `Y` | `Y` = motorcycle rider held |
| `aaa_visa` | `0` / `1` | `1` = holds an AAA-branded Visa |
| `rv_code` | `0` / `2` / `3` | RV rider level, not a flag. `0` = none; `2` and `3` correspond to `billing_plan_1.primary_rv2` / `primary_rv3` |
| `mpp_cancelled_flag` | `N` / `Y` | `Y` = membership protection plan cancelled |
| `ers_vip_member` | `N` / `Y` | `Y` = VIP roadside entitlement |

The codes give current state only. `*_start_date` and `*_stop_date` are the source for when a product was attached or dropped.
