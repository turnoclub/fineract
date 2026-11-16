# Fineract Vivriti Migration — final pipeline

File-driven, idempotent, resumable scripts to migrate loans into Fineract in three
ordered steps. Dependency-free (Python 3 stdlib only).

```
final_scripts/
  fineract_client.py            shared HTTP client (retry + throttle + helpers)
  step1_create_and_disburse.py  create clients + loans, disburse
  step2_apply_bounces.py        apply per-installment bounce charges
  step3_apply_repayments.py     push UTR repayment transactions
  run_pipeline.py               orchestrator (runs the steps in order from files)
```

## Setup

```bash
export FINERACT_BASE_URL="https://10.120.139.62:8443/fineract-provider/api/v1"
export FINERACT_TENANT="default"
export FINERACT_USER="mifos"
export FINERACT_PASSWORD="password"
export FINERACT_THROTTLE="0.1"     # seconds before each call; keep the fragile server alive
# export FINERACT_VERIFY_TLS=true  # for non-dev targets
```

## Run the whole pipeline

```bash
python3 run_pipeline.py \
  --loans   "Viviriti Disbursed Loan Data ... .csv" \
  --bounces "bounce_charges_... .csv" \
  --utr     "utr_list_... .csv" \
  [--dry-run]
```

Only the steps whose file you pass are run. **Order is fixed and matters**:
create+disburse → bounces → repayments (so each payment allocates over its bounce).
You can also set the file paths in the `CONFIG` block at the top of `run_pipeline.py`
and run it with no arguments ("pass files in code").

## Run a single step

```bash
# 1) create + disburse a file of loans (use --stop-at pending to create without disbursing)
python3 step1_create_and_disburse.py LOANS.csv --report /tmp/s1.json
python3 step1_create_and_disburse.py LOANS.csv --stop-at pending

# 2) apply bounces (disburses a still-pending loan first)
python3 step2_apply_bounces.py BOUNCES.csv --disburse-if-pending --report /tmp/s2.json

# 3) push repayments
python3 step3_apply_repayments.py UTR.csv --report /tmp/s3.json
```

## Input file formats

- **Loans** (step 1): `EXTERNAL ID, LMS_ID, ACTIVATION_DATE, FIRST_NAME, LAST_NAME,
  PRINCIPAL, LOAN TERM FREQUENCY, INTEREST RATE PER PERIOD, EXPECTED DISBURSEMENT DATE,
  SUBMITTED ON DATE`. Loan `externalId` is set to `LMS_ID`.
- **Bounces** (step 2): `loan id (=LMS), emi_number, due date (YYYY-MM-DD), total_bounce_to_apply`.
- **UTR** (step 3): `utr, payment_date (YYYY-MM-DD), total_amount, loan_ids (=LMS)`.

## Idempotency & resume

- Loans keyed by `externalId = LMS_ID`; clients de-duped by "already has a live loan".
- Bounces skipped if the installment due-date already carries one.
- Repayments keyed by `externalId = UTR` (Fineract enforces uniqueness).
- Everything is safe to re-run; step 1 also takes `--skip-done <report.json>` for a fast resume.
- All steps accept `--dry-run`, `--limit N`, `--only LMS,LMS`, `--report out.json`.

## Notes / gotchas baked in

- **Cutoff first-EMI**: loans are created without `repaymentsStartingFromDate` so the
  product's cutoff datatable derives the first EMI date.
- **Activation-vs-submission fix**: if a client's activation date is after the loan's
  submission date, the loan is submitted on the activation date.
- **Server fragility**: the client retries dropped connections / timeouts / port
  exhaustion and honours `FINERACT_THROTTLE`. For very large runs keep throttle ≥ 0.1.
- **receiptNumber > 50 chars** (e.g. cash-deposit refs) is trimmed from the start.
- **EMI<interest loans**: a small set of high-rate/48-month loans with a >30-day
  broken first period are rejected by Fineract's guard at
  `AbstractCumulativeLoanScheduleGenerator.java:320` until that guard is patched to
  exempt the broken first installment (then re-run step 1 for them).
- **De-collide UTRs**: if a UTR's externalId is locked by an earlier reversed/withdrawn
  txn, re-post with `--external-id-suffix=-r2` (the real UTR stays in receiptNumber).
```
