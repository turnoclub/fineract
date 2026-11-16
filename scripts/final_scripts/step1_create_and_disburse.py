#!/usr/bin/env python3
"""STEP 1 - create clients + loans and disburse, from a disbursed-loan CSV.

For each row: ensure the client exists (create it Active if not), create its Vivriti
loan (externalId = LMS_ID, cutoff first-EMI via the product datatable -> no
repaymentsStartingFromDate passed), then approve + disburse. With --stop-at pending
it stops after creation (no disbursal).

Idempotent / resumable:
  - a client that already owns a live Vivriti loan is left alone (no duplicate);
  - loan externalId = LMS_ID is unique, so re-creates are rejected cleanly;
  - --skip-done <report.json> skips LMS already done for a fast resume.

Data fix: if a row's ACTIVATION_DATE is after its SUBMITTED ON DATE (client not yet
active at submission), the loan is submitted on the activation date instead.

CSV columns (BOM tolerated): EXTERNAL ID, LMS_ID, ACTIVATION_DATE, FIRST_NAME,
LAST_NAME, PRINCIPAL, LOAN TERM FREQUENCY, INTEREST RATE PER PERIOD,
EXPECTED DISBURSEMENT DATE, SUBMITTED ON DATE.

Usage:
  FINERACT_* env ...
  python3 step1_create_and_disburse.py LOANS.csv [--product-id 1] [--office-id 1]
      [--stop-at active|pending] [--skip LMS,LMS] [--skip-done report.json]
      [--limit N] [--dry-run] [--report out.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime

from fineract_client import DATE_FORMAT, FineractClient, FineractError, arr_to_fineract

STRATEGY = "interest-principal-penalties-fees-order-strategy"
PRODUCT_NAME = "Turno Vivriti"


def D(s):
    return datetime.strptime(s.strip(), "%d %B %Y")


def client_id_for(client, ext):
    try:
        c = client.get(f"clients/external-id/{ext}")
        return c.get("id")
    except FineractError as e:
        if e.status == 404:
            return None
        raise


def client_has_live_loan(client, cid):
    acc = client.get(f"clients/{cid}/accounts") or {}
    for la in acc.get("loanAccounts") or []:
        st = la["status"]["value"].lower()
        if la.get("productName") == PRODUCT_NAME and "withdrawn" not in st and "rejected" not in st:
            return True
    return False


def create_client(client, row, office_id):
    body = {"officeId": office_id, "firstname": row["FIRST_NAME"].strip(),
            "lastname": (row["LAST_NAME"].strip() or row["FIRST_NAME"].strip()),
            "externalId": row["EXTERNAL ID"].strip(), "legalFormId": 1, "active": True,
            "activationDate": row["ACTIVATION_DATE"].strip(), "dateFormat": DATE_FORMAT, "locale": "en"}
    return client.post("clients", body)["clientId"]


def create_loan(client, cid, row, product_id):
    sub = max(D(row["SUBMITTED ON DATE"]), D(row["ACTIVATION_DATE"])).strftime("%d %B %Y")
    term = row["LOAN TERM FREQUENCY"].strip()
    body = {"clientId": cid, "productId": product_id, "loanType": "individual", "principal": row["PRINCIPAL"].strip(),
            "loanTermFrequency": term, "loanTermFrequencyType": 2, "numberOfRepayments": term, "repaymentEvery": 1,
            "repaymentFrequencyType": 2, "interestRatePerPeriod": row["INTEREST RATE PER PERIOD"].strip(),
            "interestRateFrequencyType": 3, "amortizationType": 1, "interestType": 0,
            "interestCalculationPeriodType": 1, "transactionProcessingStrategyCode": STRATEGY,
            "expectedDisbursementDate": row["EXPECTED DISBURSEMENT DATE"].strip(), "submittedOnDate": sub,
            "externalId": row["LMS_ID"].strip(), "dateFormat": DATE_FORMAT, "locale": "en"}
    return client.post("loans", body)["loanId"]


def disburse(client, loan_id, row):
    disb = row["EXPECTED DISBURSEMENT DATE"].strip()
    client.post(f"loans/{loan_id}?command=approve",
                {"approvedOnDate": disb, "dateFormat": DATE_FORMAT, "locale": "en"})
    client.post(f"loans/{loan_id}?command=disburse",
                {"actualDisbursementDate": disb, "transactionAmount": row["PRINCIPAL"].strip(),
                 "dateFormat": DATE_FORMAT, "locale": "en"})


def main(argv):
    ap = argparse.ArgumentParser(description="Create clients + loans and disburse.")
    ap.add_argument("loans_csv")
    ap.add_argument("--product-id", type=int, default=1)
    ap.add_argument("--office-id", type=int, default=1)
    ap.add_argument("--stop-at", choices=["active", "pending"], default="active",
                    help="active = create+disburse (default); pending = create only")
    ap.add_argument("--skip", default="", help="comma-separated LMS ids to skip (anomalies)")
    ap.add_argument("--skip-done", help="prior report json: skip LMS already created/disbursed")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}
    done = set()
    if args.skip_done:
        for x in json.load(open(args.skip_done)):
            if x.get("result") in ("created", "disbursed", "loan-exists"):
                done.add(x["lms"])
    rows = list(csv.DictReader(open(args.loans_csv, encoding="utf-8-sig")))

    report, counts, processed = [], Counter(), 0
    for r in rows:
        lms = r["LMS_ID"].strip()
        ext = r["EXTERNAL ID"].strip()
        if lms in done:
            counts["prev-done"] += 1
            continue
        if lms in skip or not ext or not r["PRINCIPAL"].strip():
            report.append({"lms": lms, "result": "skipped"}); counts["skipped"] += 1; continue
        if args.stop_at == "active" and not r["EXPECTED DISBURSEMENT DATE"].strip():
            report.append({"lms": lms, "result": "skipped-no-disb-date"}); counts["skipped"] += 1; continue
        if args.limit and processed >= args.limit:
            break
        processed += 1
        entry = {"lms": lms}
        try:
            cid = client_id_for(client, ext)
            if cid is not None and client_has_live_loan(client, cid):
                entry["result"] = "loan-exists"; counts["loan-exists"] += 1; report.append(entry); continue
            if args.dry_run:
                entry["result"] = "would-create"; counts["would-create"] += 1; report.append(entry); continue
            if cid is None:
                cid = create_client(client, r, args.office_id)
            entry["client_id"] = cid
            loan_id = create_loan(client, cid, r, args.product_id)
            entry["loan_id"] = loan_id
            if args.stop_at == "active":
                disburse(client, loan_id, r)
                entry["result"] = "disbursed"; counts["disbursed"] += 1
            else:
                entry["result"] = "created"; counts["created"] += 1
        except FineractError as e:
            entry["result"] = "error"; entry["error"] = f"HTTP {e.status}: {e.body}"; counts["error"] += 1
        report.append(entry)
        if args.report and processed % 100 == 0 and not args.dry_run:
            json.dump(report, open(args.report, "w"), indent=2)
        if processed % 200 == 0:
            print(f"  ...{processed} | {dict(counts)}", flush=True)

    if args.report:
        json.dump(report, open(args.report, "w"), indent=2)
    for r in report:
        if r["result"] == "error":
            print(f"[error] LMS {r['lms']}: {r.get('error')}")
    print(f"\nSTEP1 Summary ({'DRY-RUN' if args.dry_run else 'LIVE'}): {dict(counts)}")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
