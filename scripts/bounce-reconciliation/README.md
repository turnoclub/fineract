# Bounce-Charge Reconciliation & Old-Case Migration

Ops tooling (no change to the Fineract fork) for two tracks:

1. **`reconcile.py`** — monthly go-forward reconciliation of job-generated bounce charges.
2. **`migrate.py`** — one-time migration of old cases by replaying their full historical ledger.

Both drive Fineract's REST API; `reconcile.py` uses the native **Batch API** (`/batches`).
Full design and the verified code facts behind these scripts: `.claude/plans/tidy-brewing-diffie.md`.

## Setup

Dependency-free (Python 3 stdlib only). Configure the target via env vars:

```bash
export FINERACT_BASE_URL="https://10.120.139.62:8443/fineract-provider/api/v1"
export FINERACT_TENANT="default"
export FINERACT_USER="mifos"
export FINERACT_PASSWORD="password"
# export FINERACT_VERIFY_TLS=true   # enable cert verification for non-dev targets
```

TLS verification is **off by default** to tolerate the self-signed dev cert. Turn it on for anything
that isn't the dev box.

## 1. Monthly reconciliation — `reconcile.py`

Fill `reconciliation_template.csv` (one row per loan):

| column        | meaning                                                              |
|---------------|---------------------------------------------------------------------|
| `loan_id`     | active Fineract loan id                                             |
| `outcome`     | `full_waive` \| `partial` \| `full_collect` \| `uncollect`         |
| `amount_paid` | repayment amount (for `partial` / `full_collect`); blank otherwise |
| `payment_date`| `dd MMMM yyyy` for the repayment; blank if none                    |

Outcomes:
- **full_waive** — forgive the whole bounce.
- **partial** — post the repayment (surplus auto-allocates to the bounce), then waive the remainder.
- **full_collect** — post the repayment only; nothing to waive.
- **uncollect** — repayment already posted earlier; waive the remainder, then refund the
  already-collected portion so it rolls to next month. The refund amount is read **live** from the
  charge's `amountPaid` (never hardcoded).

Run (preview first):

```bash
python3 reconcile.py reconciliation.csv --dry-run
python3 reconcile.py reconciliation.csv
```

It executes three ordered batch passes — **A** repayments → **B** waives → **C** adjustments — so the
refunded amount lands on next month, not this one. Passes are non-atomic: a bad loan does not roll
back the rest; failures are printed per `requestId` for retry.

## 2. Old-case migration — `migrate.py`

> **Pre-flight (do once, manually):** disable job 12 *"Apply penalty to overdue loans"* (or the
> `backdate-penalties-enabled` config) for the migration window — otherwise it backdates flat ₹708
> onto every unpaid historical installment. Re-enable when done.

Describe each old loan and its full event ledger in JSON (see `migration_ledger_template.json`).
Events: `bounce` (apply charge; `ref` + `amount` + `date`→dueDate), `repayment` (`date` + `amount`),
`waive` (`ref` of a prior bounce), `adjustment` (`ref` + `amount`). Events are replayed in **date
order** (same-date tie-break: bounce → repayment → waive → adjustment) so each charge exists before
the payment that settles it.

Run (preview first):

```bash
python3 migrate.py ledger.json --dry-run
python3 migrate.py ledger.json --report migration-report.json
```

- **Idempotent:** `external_id` (your source id) is stamped on the loan; a re-run skips loans that
  already exist, so a failed/partial run is safe to repeat.
- **Fidelity:** correct end-state. Repayments and bounce due-dates are historically accurate; waiving
  a past-due charge dates to the charge's due date; **charge adjustments are stamped at the run-date**
  (rare in old data — they show in the report).
- **Verify:** add an optional `expect` block per loan (e.g. `{"outstanding": 0}`); mismatches are
  reported and exit non-zero.

## Payment id & transaction uniqueness

Repayments can carry payment details (both scripts support this):

| field             | maps to         | purpose                                                        |
|-------------------|-----------------|----------------------------------------------------------------|
| `payment_type_id` | `paymentTypeId` | the payment method (FK to `m_payment_type`; e.g. `1` on dev)    |
| `payment_ref`     | `receiptNumber` | the NACH/UTR/txn reference (recorded only)                     |
| `external_id`     | `externalId`    | the **unique** transaction id                                  |

**Fineract only deduplicates on `externalId`** — there is a DB unique constraint
(`external_id_UNIQUE` on `m_loan_transaction`); a duplicate post returns a clean validation error
(`externalId value.must.be.unique`), not a 500. It does **not** dedupe by amount/date. So:

- Set `external_id` = your payment id to guarantee a payment can't be double-posted (re-runs are safe).
- Without `external_id`, identical repayments will both succeed and create duplicates.

List configured payment types with `GET /paymenttypes`; create one (e.g. "NACH") via
`POST /paymenttypes` if needed.

## Notes / limits

- Charges cannot be `PUT`-updated or `DELETE`d on an **active** loan — reconcile only via
  waive / adjustment. (Both scripts honour this.)
- Run loans concurrently across the file if needed, but keep each loan's events strictly ordered.
- Migration idempotency is **loan-level** (skip if the loan `external_id` exists). A repayment's
  `external_id` additionally protects that single payment from being double-posted. Fully resumable
  mid-loan replay (idempotent charges/waives too) would need `external_id` on those events — not yet
  implemented; if a loan errors mid-replay, clean up the partial loan before re-running.
