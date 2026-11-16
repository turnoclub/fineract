#!/usr/bin/env python3
"""Disburse pending created loans (mark them Active), driven by a loan-id list.

For loans created by create_clients_and_loans.py (externalId = LMS_ID). Resolves
each by loans/external-id/{lms}; if "Submitted and pending approval" it approves +
disburses at the loan's own expectedDisbursementDate for its principal. Already-active
loans are skipped (idempotent). No charges, no repayments -- disbursal only.

Target loan ids come from --utr (the file's unique loan_ids) or --lms.

Usage:
  FINERACT_* env (see fineract_client.py) ...
  python3 disburse_loans_v2.py --utr UTR.csv [--only ...] [--limit N] [--dry-run] [--report out.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter

from fineract_client import FineractClient, FineractError

PENDING = "submitted and pending approval"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def arr_to_fineract(a):
    return f"{a[2]:02d} {MONTHS[a[1] - 1]} {a[0]}"


def main(argv):
    ap = argparse.ArgumentParser(description="Disburse pending created loans (by externalId=LMS).")
    ap.add_argument("--utr", help="UTR csv -> targets = its unique loan_ids")
    ap.add_argument("--lms", default="", help="comma-separated LMS ids (alternative to --utr)")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    targets = set()
    if args.utr:
        for r in csv.DictReader(open(args.utr, encoding="utf-8-sig")):
            # a UTR row may span multiple loans: loan_ids = "a, b, c"
            for lid in r["loan_ids"].replace(";", ",").split(","):
                if lid.strip():
                    targets.add(lid.strip())
    targets |= {x.strip() for x in args.lms.split(",") if x.strip()}
    if args.only:
        only = {x.strip() for x in args.only.split(",") if x.strip()}
        targets &= only
    targets = sorted(targets)

    report = []
    counts = Counter()
    processed = 0
    for lms in targets:
        if args.limit and processed >= args.limit:
            break
        processed += 1
        entry = {"lms": lms}
        try:
            try:
                loan = client.get(f"loans/external-id/{lms}")
            except FineractError as e:
                if e.status == 404:
                    entry["result"] = "loan-not-created"; counts["no-loan"] += 1; report.append(entry); continue
                raise
            loan_id = loan["id"]; entry["loan_id"] = loan_id
            status = loan["status"]["value"]
            if status.lower() != PENDING:
                entry["result"] = f"skip ({status})"; counts["skip"] += 1; report.append(entry); continue
            disb = arr_to_fineract(loan["timeline"]["expectedDisbursementDate"])
            principal = loan["principal"]
            entry["disburse"] = f"{disb} amt={principal}"
            if not args.dry_run:
                client.post(f"loans/{loan_id}?command=approve",
                            {"approvedOnDate": disb, "dateFormat": "dd MMMM yyyy", "locale": "en"})
                client.post(f"loans/{loan_id}?command=disburse",
                            {"actualDisbursementDate": disb, "transactionAmount": principal,
                             "dateFormat": "dd MMMM yyyy", "locale": "en"})
            entry["result"] = "disbursed" if not args.dry_run else "would-disburse"
            counts[entry["result"]] += 1
        except FineractError as e:
            entry["result"] = "error"; entry["error"] = f"HTTP {e.status}: {e.body}"; counts["error"] += 1
        report.append(entry)
        if args.report and processed % 100 == 0 and not args.dry_run:
            json.dump(report, open(args.report, "w"), indent=2)
        if processed % 200 == 0:
            print(f"  ...{processed}/{len(targets)} | {dict(counts)}", flush=True)

    if args.report:
        json.dump(report, open(args.report, "w"), indent=2)
    for r in report:
        if r["result"] == "error":
            print(f"[error] LMS {r['lms']} {r.get('error','')}")
    print(f"\nSummary ({'DRY-RUN' if args.dry_run else 'LIVE'}): {dict(counts)}")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
