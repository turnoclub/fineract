#!/usr/bin/env python3
"""Approve + disburse a set of pending Vivriti loans from the mapping sheet.

Disburse-only companion to disburse_and_apply_bounces.py, for loans that have no
bounce rows (so the bounce-driven script never touched them) but do need disbursing
before their UTR repayments can be applied.

Target loans = the unique LMS ids referenced by --utr (or --lms). For each:
  - resolve the Fineract loan id from the saved lms_fineract mapping CSV,
  - if "Submitted and pending approval": approve + disburse at the Vivriti sheet's
    EXPECTED DISBURSEMENT DATE / PRINCIPAL,
  - if already Active: skip (idempotent).

Usage:
  FINERACT_* env (see fineract_client.py) ...
  python3 disburse_loans.py VIVRITI_SHEET.csv lms_fineract_mapping.csv \
      --utr UTR.csv [--only 90179,95705] [--dry-run] [--report out.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter

from fineract_client import FineractClient, FineractError

PENDING = "submitted and pending approval"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Approve + disburse pending Vivriti loans.")
    ap.add_argument("vivriti_sheet", help="Viviriti Disbursed Loan Data sheet (disbursement params)")
    ap.add_argument("mapping_csv", help="lms_fineract_mapping.csv (FINERACT_LOAN_ID per LMS)")
    ap.add_argument("--utr", help="UTR csv -> target LMS ids are its unique loan_ids")
    ap.add_argument("--lms", default="", help="comma-separated LMS ids (alternative to --utr)")
    ap.add_argument("--only", default="", help="further restrict to these LMS ids")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    params = {r["LMS_ID"].strip(): r for r in csv.DictReader(open(args.vivriti_sheet))}
    loan_id_by_lms = {r["LMS_ID"].strip(): r["FINERACT_LOAN_ID"].strip()
                      for r in csv.DictReader(open(args.mapping_csv)) if r.get("FINERACT_LOAN_ID")}

    targets: set[str] = set()
    if args.utr:
        for r in csv.DictReader(open(args.utr)):
            targets.add(r["loan_ids"].strip())
    targets |= {x.strip() for x in args.lms.split(",") if x.strip()}
    if args.only:
        only = {x.strip() for x in args.only.split(",") if x.strip()}
        targets &= only
    targets = sorted(targets)

    report = []
    counts = Counter()
    for lms in targets:
        entry = {"lms": lms}
        loan_id = loan_id_by_lms.get(lms)
        if not loan_id:
            entry["result"] = "no-fineract-loan"; counts["no-loan"] += 1; report.append(entry); continue
        entry["fineract_loan_id"] = loan_id
        if lms not in params:
            entry["result"] = "no-mapping-params"; counts["no-params"] += 1; report.append(entry); continue
        try:
            status = client.get(f"loans/{loan_id}")["status"]["value"]
        except FineractError as e:
            entry["result"] = f"status-error-{e.status}"; counts["error"] += 1; report.append(entry); continue
        if status.lower() != PENDING:
            entry["result"] = f"skip ({status})"; counts["skip-not-pending"] += 1; report.append(entry); continue

        disb = params[lms]["EXPECTED DISBURSEMENT DATE"].strip()
        principal = params[lms]["PRINCIPAL"].strip()
        entry["actions"] = [f"approve@{disb}", f"disburse@{disb} amount={principal}"]
        if args.dry_run:
            entry["result"] = "would-disburse"; counts["would-disburse"] += 1; report.append(entry); continue
        try:
            client.post(f"loans/{loan_id}?command=approve",
                        {"approvedOnDate": disb, "dateFormat": "dd MMMM yyyy", "locale": "en"})
            client.post(f"loans/{loan_id}?command=disburse",
                        {"actualDisbursementDate": disb, "transactionAmount": principal,
                         "dateFormat": "dd MMMM yyyy", "locale": "en"})
            entry["result"] = "disbursed"; counts["disbursed"] += 1
        except FineractError as e:
            entry["result"] = "error"; entry["error"] = f"HTTP {e.status}: {e.body}"; counts["error"] += 1
        report.append(entry)

    if args.report:
        json.dump(report, open(args.report, "w"), indent=2)
    for r in report:
        if r["result"] not in ("disbursed", "would-disburse", "skip (Active)"):
            print(f"[{r['result']}] LMS {r['lms']} {r.get('error','')}")
    print(f"\nSummary ({'DRY-RUN' if args.dry_run else 'LIVE'}): {dict(counts)}")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
