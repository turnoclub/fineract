# Collections report — provisioning

Recreates the **Collections** custom report (and its prerequisite payment types) in any
Fineract environment via the runtime API. No code change / no migration — these are
configuration objects stored per tenant, so they must be created in each environment
(dev / prod) once.

## Files
- `create_collections_report.json` — body for `POST /v1/reports` (the report + its
  `startDate`/`endDate` parameters).
- `create_payment_types.json` — the instrument types the report's `instrument_type`
  column reads back (`m_payment_type.value`). Each element is a `POST /v1/paymenttypes` body.

## Prerequisites / assumptions
- The `startDate` / `endDate` stretchy parameters are the standard Fineract seeds
  (`parameterId` **1** = startDate, **2** = endDate). These exist in every Fineract DB by
  default; if a custom env differs, check `GET /v1/reports/template` and adjust the
  `parameterId`s in `create_collections_report.json`.
- The report SQL targets standard + investor-module tables and the `m_loan_charge_paid_by`
  per-installment split. `foreclosure` = `Y` when the loan is foreclosed
  (`loan_sub_status_id = 100`) and the row's transaction is the closing payment
  (`transaction_date = closedon_date`); otherwise `N`.

## Provision in prod

Set the prod target (do NOT commit real credentials):

```bash
BASE="https://<PROD_HOST>:<PORT>/fineract-provider/api/v1"
TENANT="default"
AUTH="-u <USER>:<PASSWORD>"        # or a bearer token
TLS="-k"                            # drop if the prod cert is trusted
H=(-H "Fineract-Platform-TenantId: $TENANT" -H "Content-Type: application/json" -H "Accept: application/json")
```

1) Payment types (skip any that already exist — `GET $BASE/paymenttypes` to check):

```bash
# create each element of create_payment_types.json
python3 - "$BASE" <<'PY'
import json,sys,subprocess
base=sys.argv[1]
for body in json.load(open("create_payment_types.json")):
    print(body["name"], "->")
    subprocess.run(["curl","-sk",*"-u USER:PASSWORD".split(),
        "-H","Fineract-Platform-TenantId: default","-H","Content-Type: application/json",
        "-X","POST",f"{base}/paymenttypes","-d",json.dumps(body),"-w","\n%{http_code}\n"])
PY
```

Or one-off with curl:

```bash
curl $TLS $AUTH "${H[@]}" -X POST "$BASE/paymenttypes" \
  -d '{"name":"ENACH","description":"E-NACH auto-debit","isCashPayment":false,"position":2}'
curl $TLS $AUTH "${H[@]}" -X POST "$BASE/paymenttypes" \
  -d '{"name":"Payment Link","description":"Hosted payment link (e.g. Razorpay)","isCashPayment":false,"position":3}'
```

2) The report:

```bash
curl $TLS $AUTH "${H[@]}" -X POST "$BASE/reports" \
  --data @create_collections_report.json -w "\n%{http_code}\n"
```

`200 {"resourceId": <id>}` means success (the `id` is the prod report id).

## Run / download

```bash
curl $TLS $AUTH "${H[@]}" \
  "$BASE/runreports/Collections?exportCSV=true&R_startDate=2026-06-10&R_endDate=2026-06-12"
```

Returns `text/csv` with the 22 Collections columns. Same report is editable in the UI under
**Admin → System → Manage Reports** (`PUT /v1/reports/{id}` to update the SQL).

## `loanIds` filter

`R_loanIds` takes a comma-separated list and matches **either** the internal loan id
(`m_loan.id`) or the external id the report prints as `client_loan_id`. Spaces are stripped,
so `5230, 5231` is fine.

```bash
# just two loans
...&R_startDate=2026-07-01&R_endDate=2026-07-31&R_loanIds=5230,5231

# every loan in the window (the previous behaviour)
...&R_startDate=2026-07-01&R_endDate=2026-07-31&R_loanIds=
```

**The parameter is optional — omitting it returns all loans**, so existing callers keep
working unchanged.

That needed care. Substitution only happens for params actually sent
(`AbstractReportingProcessService.getReportParams`), so when `R_loanIds` is absent the literal
text `${loanIds}` survives into the SQL. That is still *valid* SQL — a string literal — so the
query does not fail; every branch just evaluates false and it silently returns **zero rows**.
A silently empty report is far more dangerous than an error.

The guard is this branch:

```sql
OR '${loanIds}' = CONCAT('$', '{loanIds}')
```

It is true only when the token was never substituted. The right-hand side is written as
`CONCAT('$', '{loanIds}')` precisely so the source text does not itself contain the literal
token `${loanIds}` and therefore survives `replace()` untouched. When a value *is* supplied,
the comparison is `'5230' = '${loanIds}'`, which is false, and normal filtering applies.
If you ever rewrite this clause, keep the two halves split — collapsing it to `'${loanIds}'`
makes it self-substitute and the report silently returns everything.

It is deliberately not registered in `m_report_parameter`: the SQL substitution works for any
`R_*` query param regardless of registration, and registering it would need a new
`stretchy_parameter` row. The trade-off is that the UI's report screen renders no input box
for it — this filter is API-only. Add the stretchy parameter if the UI needs it.

The clause uses `CONCAT`/`LIKE` rather than `FIND_IN_SET` so it works on both MySQL and
PostgreSQL. On a MySQL-only deployment it can be simplified to
`FIND_IN_SET(ml.id, REPLACE('${loanIds}', ' ', '')) > 0`.

To apply to an environment that already has the report:

```bash
curl $TLS $AUTH "${H[@]}" -X PUT "$BASE/reports/<REPORT_ID>" \
  --data @update_collections_report_loanids.json -w "\n%{http_code}\n"
```
