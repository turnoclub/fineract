# Reverting a wrongly-applied foreclosure — detailed runbook

Step-by-step procedure for undoing a foreclosure that should not have happened and restoring
the loan's original repayment schedule.

Verified end-to-end against a real correction: **loan 5230** (client 5316, product Turno
Vivriti), foreclosed in error on 2026-07-31 and fully restored on 2026-08-03. Every response
body shown below is from that run unless marked otherwise.

---

## Background: why this is not a one-call fix

Reversing the foreclosure repayment restores **balances** but not the **schedule**.
Foreclosure destroys the installment rows outright — `Loan.java:1606`:

```java
public void updateLoanScheduleOnForeclosure(final Collection<LoanRepaymentScheduleInstallment> installments) {
    this.repaymentScheduleInstallments.clear();      // originals deleted, not flagged
    for (final LoanRepaymentScheduleInstallment installment : installments) {
        addLoanRepaymentScheduleInstallment(installment);
    }
}
```

The surviving schedule is: the already-due installments, plus **one balloon installment**
holding all remaining principal, dated on the foreclosure date. There is no snapshot and no
`LoanTermVariations` row, so nothing can be un-reversed — the schedule must be **regenerated**.

Foreclosure additionally:
- deactivates charges due after the foreclosure date (`loanCharge.setActive(false)`);
- pulls `expectedMaturityDate` / `actualMaturityDate` in to the foreclosure date;
- recalculates interest only up to the foreclosure date, so `totalExpectedRepayment` collapses.

**Why a reschedule rather than `undoDisbursal` + re-disburse:** undo-disbursal also regenerates
the schedule, but wipes the disbursement, accruals, penalties and transaction history, leaves
no record that a correction happened, and gives no preview. A reschedule preserves everything,
writes an audit trail, and can be previewed before it commits.

---

## Environment

```bash
BASE="http://10.120.139.62:4200/fineract-provider/api/v1"
TENANT="Fineract-Platform-TenantId: default"
AUTH="Authorization: Basic $(printf 'mifos:password' | base64)"
JSON="Content-Type: application/json"
LOAN_ID=5230
```

All dates below use `"dateFormat":"dd MMMM yyyy"` with `"locale":"en"`. Fineract requires both
alongside any date field.

---

## STEP 1 — Capture the baseline

You need the "before" picture to compute the target and to verify the result.

**API** `GET /v1/loans/{loanId}?associations=repaymentSchedule,transactions`

```bash
curl -s "$BASE/loans/$LOAN_ID?associations=repaymentSchedule,transactions" \
  -H "$TENANT" -H "$AUTH" -o before.json
```

**Record these fields:**

| Field | Loan 5230 (broken) | Why |
|---|---|---|
| `status.value` | `Active` | Must be Active, not charged-off |
| `numberOfRepayments` | `36` | **The true term — untouched by foreclosure** |
| `repaymentEvery` + `repaymentFrequencyType` | `1` / `Months` | Rebuilds the date grid |
| `annualInterestRate` | `23.0` | For the EMI cross-check |
| `timeline.expectedMaturityDate` | `2026-07-07` | Wrong; should return to original |
| `summary.totalExpectedRepayment` | `324,562` | Wrong; will rise |
| `summary.penaltyChargesCharged` | `2,124` | Must survive the correction |
| `repaymentSchedule.periods[]` | 4 installments | The truncated schedule |

**Diagnosis — you have this problem if:** the installment count is far below
`numberOfRepayments`, the last installment is a large balloon dated on the foreclosure date,
and `totalExpectedRepayment` is well below principal + expected interest.

Loan 5230's truncated schedule:

```
  # fromDate    dueDate      principal   interest   penalty      total
  1 2026-03-31  2026-05-07      5863.0     7092.0     708.0    13663.0
  2 2026-05-07  2026-06-07      5975.0     5638.0     708.0    12321.0
  3 2026-06-07  2026-07-07      6090.0     5523.0     708.0    12321.0
  4 2026-07-07  2026-07-31    282072.0     4185.0         0   286257.0   <-- balloon
```

---

## STEP 2 — Locate the foreclosure transaction

No extra API call — read it from `before.json`.

The foreclosure repayment is the transaction whose `type.value` is `Repayment`, whose date is
the foreclosure date, and whose amount equals the full payoff (principal + interest + penalty).

Loan 5230:

```
    id date        type            amount  principal  interest  penalty  reversed
113723 2026-07-31  Repayment     325699.0   300000.0   22438.0   2124.0     False
```

Note the id — that is `$TXN_ID`. Also confirm `manuallyReversed` is currently `false`; if it is
already `true`, skip to Step 4.

Cross-check: `loanSubStatus` is `Foreclosed` (id 100) and `timeline.closedOnDate` equals the
foreclosure date.

---

## STEP 3 — Reverse the foreclosure transaction

**API** `POST /v1/loans/{loanId}/transactions/{transactionId}`

```bash
curl -s -X POST "$BASE/loans/$LOAN_ID/transactions/113723" \
  -H "$TENANT" -H "$AUTH" -H "$JSON" \
  -d '{"transactionAmount":0,"transactionDate":"31 July 2026","dateFormat":"dd MMMM yyyy","locale":"en"}'
```

| Field | Value | Notes |
|---|---|---|
| `transactionAmount` | `0` | **Zero = pure reversal**, no replacement transaction |
| `transactionDate` | foreclosure date | Same date as the original transaction |
| `dateFormat`, `locale` | required | |

**No `command` query param.** The endpoint defaults to adjust — `adjustTransaction` is the
`else` branch in `LoanTransactionsApiResource.adjustTransaction`. And zero means reversal
because `LoanWritePlatformServiceJpaRepositoryImpl.adjustLoanTransaction` computes
`isAdjustCommand = (transactionAmount.compareTo(BigDecimal.ZERO) > 0)` — a positive amount
would create a replacement transaction instead.

This step can also be done from the UI (loan → Transactions → the repayment → Undo), which is
how it was done for 5230; the API above is the equivalent.

---

## STEP 4 — Confirm the reversal, and confirm the schedule is still broken

**API** `GET /v1/loans/{loanId}?associations=repaymentSchedule,transactions`

```bash
curl -s "$BASE/loans/$LOAN_ID?associations=repaymentSchedule,transactions" -H "$TENANT" -H "$AUTH"
```

**Expect — reversal succeeded:**

| Field | Expected |
|---|---|
| transaction `manuallyReversed` | `true` |
| `summary.principalPaid` | `0.0` |
| `summary.totalRepayment` | `0.0` |
| `summary.principalOutstanding` | back to full principal (`300000.0`) |
| `status.value` | `Active` |

**Expect — schedule still wrong** (this is the point of the step): still 4 installments, still
a balloon, `expectedMaturityDate` still `2026-07-07`. If the schedule *is* somehow correct
here, stop — you do not need the rest of this runbook.

---

## STEP 5 — Compute the target schedule

No API. Arithmetic, and it is what tells you the reschedule will *restore* the original rather
than invent a new one. Do not skip it.

**5a. `rescheduleFromDate`** = the balloon installment's `dueDate` = `31 July 2026`.
This identifies the installment and must match an existing due date **exactly**.

**5b. `adjustedDueDate`** = where that installment *should* fall on the original grid.
Installments ran on the 7th; the preceding installment was due 2026-07-07 and the frequency is
monthly, so the target is **`07 August 2026`**.

**5c. Verify the balloon principal.** It must equal original principal minus principal already
scheduled:

```
300,000 − (5,863 + 5,975 + 6,090) = 282,072   ✔ matches the balloon exactly
```

**5d. Verify the EMI.** Re-amortising that balance over the remaining installments must
reproduce the original EMI:

```
remaining installments = 36 − 3 = 33
r = 23% / 12 = 0.0191667
EMI = 282,072 × r / (1 − (1+r)^−33) = 11,613
```

Compare against installments 2 and 3 of the existing schedule: 5,975 + 5,638 = **11,613**, and
6,090 + 5,523 = **11,613**. They agree, so the reschedule will land on the original schedule.

**If 5c or 5d disagree**, stop and investigate — the loan may have had genuine repayments or
prior restructuring, and this runbook's assumptions do not hold.

---

## STEP 6 — Check a reschedule reason exists

`rescheduleReasonId` is **mandatory**: `validateRescheduleReasonId` applies
`.notNull().integerGreaterThanZero()`.

**API** `GET /v1/rescheduleloans/template`

```bash
curl -s "$BASE/rescheduleloans/template" -H "$TENANT" -H "$AUTH"
```

If `rescheduleReasons` is `[]`, none is configured — continue to Step 7. On the 5230 tenant it
was empty.

**API** `GET /v1/codes` — find the code id

```bash
curl -s "$BASE/codes" -H "$TENANT" -H "$AUTH"
```
```json
{"id": 23, "name": "LoanRescheduleReason", "systemDefined": true}
```

**API** `GET /v1/codes/{codeId}/codevalues` — list existing values

```bash
curl -s "$BASE/codes/23/codevalues" -H "$TENANT" -H "$AUTH"
```

If a usable value already exists, note its `id` and skip Step 7.

---

## STEP 7 — Create the reschedule reason (one-off per environment)

**API** `POST /v1/codes/{codeId}/codevalues`

```bash
curl -s -X POST "$BASE/codes/23/codevalues" \
  -H "$TENANT" -H "$AUTH" -H "$JSON" \
  -d '{"name":"Foreclosure reversal correction",
       "description":"Restore schedule after foreclosure was reverted",
       "position":0,
       "isActive":true}'
```

Returns `{"resourceId": <REASON_ID>}`. Only `name` is functionally significant — it is what
appears in the reschedule dropdown and is stored against the request.

Equivalent UI path: **Admin → System → Manage Codes → LoanRescheduleReason → Add Code Value**.
Watch for leading/trailing spaces if you type it in the UI — 5230's value was stored as
`" Foreclosure reversal correction    "`. Cosmetic, but untidy.

Re-run `GET /v1/codes/23/codevalues` to confirm and capture the id (it was **23** here —
coincidentally the same number as the code id; do not assume that).

---

## STEP 8 — Create the reschedule request

**API** `POST /v1/rescheduleloans`

```bash
curl -s -X POST "$BASE/rescheduleloans" \
  -H "$TENANT" -H "$AUTH" -H "$JSON" \
  -d '{
        "loanId": 5230,
        "rescheduleFromDate": "31 July 2026",
        "adjustedDueDate": "07 August 2026",
        "rescheduleReasonId": 23,
        "submittedOnDate": "03 August 2026",
        "dateFormat": "dd MMMM yyyy",
        "locale": "en"
      }'
```

| Field | Value | Notes |
|---|---|---|
| `loanId` | `5230` | |
| `rescheduleFromDate` | balloon due date (5a) | Must match an existing installment due date exactly |
| `adjustedDueDate` | original grid date (5b) | Also satisfies the "at least one change" rule |
| `rescheduleReasonId` | from Step 6/7 | Mandatory |
| `submittedOnDate` | today | Not in the future |
| `extraTerms` | **omit** | See Trap 1 |

**Response:**
```json
{"officeId":1,"clientId":5316,"loanId":5230,"resourceId":2}
```

`resourceId` is `$RESCHEDULE_ID`. The request is now **Submitted and pending approval** —
nothing on the loan has changed.

---

## STEP 9 — Preview

**API** `GET /v1/rescheduleloans/{id}?command=previewLoanReschedule`

```bash
curl -s "$BASE/rescheduleloans/2?command=previewLoanReschedule" -H "$TENANT" -H "$AUTH"
```

Returns a `LoanScheduleModel` — the schedule **as it would be** after approval. Still nothing
committed.

---

## STEP 10 — Verify the preview (the gate)

Check every row against the baseline from Step 1 and the targets from Step 5:

| Check | Loan 5230 preview |
|---|---|
| Installment count == `numberOfRepayments` | 36 ✔ |
| Earlier installments **byte-identical** | 5863/7092/708, 5975/5638/708, 6090/5523/708 ✔ |
| Balloon replaced by normal installment on `adjustedDueDate` | #4 due 2026-08-07, 6207+5406 ✔ |
| EMI == Step 5d | 11,613 ✔ |
| Last due date == original maturity | 2029-04-07 ✔ |
| Closing balance | 0 ✔ |
| Penalties preserved | 2,124 ✔ |
| Totals reconcile | 300,000 + 119,405 + 2,124 = 421,529 ✔ |

The **identical-earlier-installments** check is the important one when the schedule generator
has local modifications — it proves regeneration is not drifting. On 5230 the broken first
period (2026-03-31 → 2026-05-07, ₹7,092 interest) came back exactly as stored despite
uncommitted changes to `BrokenPeriodDecliningBalanceLoanScheduleGenerator` and
`LoanApplicationTerms`.

If **any** row disagrees → Step 11b (reject), fix the inputs, and return to Step 8.

---

## STEP 11a — Approve

**API** `POST /v1/rescheduleloans/{id}?command=approve`

```bash
curl -s -X POST "$BASE/rescheduleloans/2?command=approve" \
  -H "$TENANT" -H "$AUTH" -H "$JSON" \
  -d '{"approvedOnDate":"03 August 2026","dateFormat":"dd MMMM yyyy","locale":"en"}'
```

```json
{"officeId":1,"clientId":5316,"loanId":5230,"resourceId":2,
 "changes":{"approvedOnDate":"03 August 2026","approvedByUserId":1}}
```

**This is the irreversible step** — it clears and rewrites `m_loan_repayment_schedule` and
reprocesses transactions. There is no un-approve.

## STEP 11b — Reject (if the preview was wrong)

**API** `POST /v1/rescheduleloans/{id}?command=reject`

```bash
curl -s -X POST "$BASE/rescheduleloans/1?command=reject" \
  -H "$TENANT" -H "$AUTH" -H "$JSON" \
  -d '{"rejectedOnDate":"03 August 2026","dateFormat":"dd MMMM yyyy","locale":"en"}'
```

```json
{"officeId":1,"clientId":5316,"loanId":5230,"resourceId":1,
 "changes":{"rejectedOnDate":"03 August 2026","rejectedByUserId":1}}
```

Costs nothing — the loan is untouched and you can create another request. Rejected requests
stay visible as audit history.

**API** `GET /v1/rescheduleloans?loanId={loanId}` — check status of all requests on a loan

```bash
curl -s "$BASE/rescheduleloans?loanId=5230" -H "$TENANT" -H "$AUTH"
```

---

## STEP 12 — Final verification

**API** `GET /v1/loans/{loanId}?associations=repaymentSchedule`

```bash
curl -s "$BASE/loans/$LOAN_ID?associations=repaymentSchedule" -H "$TENANT" -H "$AUTH"
```

| Field | Before | After |
|---|---|---|
| installments | 4 | **36** |
| `numberOfRepayments` | 36 | 36 |
| `timeline.expectedMaturityDate` | 2026-07-07 | **2029-04-07** |
| `timeline.actualMaturityDate` | 2026-07-07 | **2029-04-07** |
| `summary.interestCharged` | 22,438 | **119,405** |
| `summary.totalExpectedRepayment` | 324,562 | **421,529** |
| `summary.penaltyChargesCharged` | 2,124 | 2,124 |
| last installment | balloon 286,257 on 07-31 | 11,608 on 2029-04-07 |

Confirm the figures match what the preview promised. If they do not, the preview and the commit
diverged — raise it rather than patching further.

---

## STEP 13 — Post-correction follow-ups

- **`totalOverdue` will not refresh by itself.** It is maintained by the *Update Loan Arrears
  Ageing* job (job 2). On 5230 it still read 38,305 immediately after the fix.
- **Penalties resume from the corrected dates.** Installments now have real due dates again, so
  the *Apply penalty to overdue loans* job (job 12) will assess them normally on its next run.
- Both jobs live in **scheduler group 0**. If that group's Quartz scheduler is frozen, neither
  runs and both figures stay stale — check `GET /v1/jobs` and compare `nextRunTime` against the
  clock before assuming the numbers are wrong.

---

## Traps

**Trap 1 — never pass `extraTerms`.** Foreclosure truncates the *schedule* but leaves
`m_loan.number_of_repayments` untouched, and the reschedule engine builds `LoanApplicationTerms`
from the loan, not from the surviving rows. `extraTerms` is therefore **additive on top of the
original count**. Sizing `extraTerms: 32` against 5230's 4 remaining rows produced **68**
installments running to 2031-12-07 at an EMI of 7,627 — that was request 1, rejected. Omitting
it gave the correct 36. `adjustedDueDate` alone satisfies `validateIsThereAnyIncomingChange`,
which requires only *one* of `graceOnPrincipal`, `graceOnInterest`, `extraTerms`,
`newInterestRate`, `adjustedDueDate` or `emi`.

**Trap 2 — overdue charges can close this route entirely.** `validateForOverdueCharges` fails
with `not.allowed.due.to.overdue.charges` if any overdue-installment charge is due *after* the
reschedule installment's `fromDate`. On 5230 the penalties were due 05-07, 06-07 and 07-07
against a `fromDate` of 07-07 — none strictly after, so it passed by a single day. **Do the
correction before the overdue-penalty job applies another charge**, or you are left with
undo-disbursal.

**Trap 3 — `rescheduleFromDate` must match an installment due date exactly**, and that
installment must not have obligations met (`validateReschedulingInstallment`, which fails with
`repayment.schedule.installment.does.not.exist` or
`repayment.schedule.installment.obligation.met`). There is no "must be a future installment"
rule — a balloon dated today is fine.

**Trap 4 — progressive-schedule loans take a different validator path**
(`progressiveLoanRescheduleRequestDataValidatorDelegate`). This runbook is verified for
cumulative / declining-balance loans only.

**Trap 5 — charged-off loans are rejected outright** with `error.msg.loan.is.charged.off`.

---

## Fallback: `emi` + `endDate`

If the preview still comes out wrong, pin both ends explicitly instead of letting the generator
derive the term. Both fields are required together (`validateEMIAndEndDate`), and `endDate` must
not precede `rescheduleFromDate`:

```json
{
  "loanId": 5230,
  "rescheduleFromDate": "31 July 2026",
  "adjustedDueDate": "07 August 2026",
  "emi": 11613,
  "endDate": "07 April 2029",
  "rescheduleReasonId": 23,
  "submittedOnDate": "03 August 2026",
  "dateFormat": "dd MMMM yyyy",
  "locale": "en"
}
```

Not exercised on 5230 — the `adjustedDueDate`-only form was correct on the second attempt.

---

## API summary

| Step | Method | Endpoint | Commits? |
|---|---|---|---|
| 1, 4 | `GET` | `/v1/loans/{loanId}?associations=repaymentSchedule,transactions` | no |
| — | `GET` | `/v1/loans/external-id/{externalId}` | no |
| 3 | `POST` | `/v1/loans/{loanId}/transactions/{transactionId}` | **yes** |
| 6 | `GET` | `/v1/rescheduleloans/template` | no |
| 6 | `GET` | `/v1/codes` | no |
| 6 | `GET` | `/v1/codes/{codeId}/codevalues` | no |
| 7 | `POST` | `/v1/codes/{codeId}/codevalues` | yes (config only) |
| 8 | `POST` | `/v1/rescheduleloans` | no (pending) |
| 9 | `GET` | `/v1/rescheduleloans/{id}?command=previewLoanReschedule` | no |
| 11a | `POST` | `/v1/rescheduleloans/{id}?command=approve` | **yes, irreversible** |
| 11b | `POST` | `/v1/rescheduleloans/{id}?command=reject` | no |
| 11b | `GET` | `/v1/rescheduleloans?loanId={loanId}` | no |
| 12 | `GET` | `/v1/loans/{loanId}?associations=repaymentSchedule` | no |
| 13 | `GET` | `/v1/jobs` | no |

Steps 6–7 are one-off per environment. Only three calls in the whole procedure change the loan:
Step 3 (reversal), Step 8 (creates a pending request), Step 11a (commits).
