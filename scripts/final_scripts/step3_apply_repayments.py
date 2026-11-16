#!/usr/bin/env python3
"""STEP 3 - push repayment transactions, from a UTR CSV.

Resolves each loan by externalId = LMS_ID and posts one repayment per UTR row:
  transactionDate = payment_date , transactionAmount = total_amount ,
  paymentTypeId = --payment-type-id , receiptNumber = utr (capped 50) , externalId = utr.

Fineract enforces uniqueness on transaction externalId, so the UTR makes each payment
idempotent — a re-run skips an already-posted UTR instead of double-posting. Only
Active loans accept repayments; pending / missing / already-posted rows are reported.

Apply bounces (STEP 2) BEFORE repayments so each payment allocates over the bounce too.

UTR CSV (BOM tolerated): utr , payment_date (YYYY-MM-DD) , total_amount , loan_ids (LMS id).

Usage:
  FINERACT_* env ...
  python3 step3_apply_repayments.py UTR.csv [--payment-type-id 1]
      [--external-id-suffix=-r2] [--only LMS,LMS] [--limit N] [--dry-run] [--report out.json]
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter

from fineract_client import (FineractClient, FineractError, is_duplicate_external_id, iso_to_fineract,
                             repayment_body, resolve_loan_by_lms)


def main(argv):
    ap = argparse.ArgumentParser(description="Push UTR repayments by externalId=LMS.")
    ap.add_argument("utr_csv")
    ap.add_argument("--payment-type-id", default="1")
    ap.add_argument("--external-id-suffix", default="",
                    help="append to externalId to de-collide UTRs locked by reversed/withdrawn txns")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    rows = list(csv.DictReader(open(args.utr_csv, encoding="utf-8-sig")))
    rows.sort(key=lambda r: (r["loan_ids"].strip(), r["payment_date"].strip()))  # in-order per loan
    only = {x.strip() for x in args.only.split(",") if x.strip()}

    status_cache, report, counts, processed = {}, [], Counter(), 0

    def status_of(loan):
        return loan["status"]["value"]

    loan_cache = {}

    def get_loan(lms):
        if lms not in loan_cache:
            loan_cache[lms] = resolve_loan_by_lms(client, lms)
        return loan_cache[lms]

    for r in rows:
        lms = r["loan_ids"].strip()
        utr = r["utr"].strip()
        if only and lms not in only:
            continue
        if args.limit and processed >= args.limit:
            break
        processed += 1
        entry = {"utr": utr, "lms": lms, "amount": r["total_amount"].strip(), "date": r["payment_date"].strip()}
        try:
            loan = get_loan(lms)
            if loan is None:
                entry["result"] = "loan-not-created"; counts["no-loan"] += 1; report.append(entry); continue
            if status_of(loan) != "Active":
                entry["result"] = f"skip ({status_of(loan)})"; counts["skip-not-active"] += 1; report.append(entry); continue
            if args.dry_run:
                entry["result"] = "would-post"; counts["would-post"] += 1; report.append(entry); continue
            body = repayment_body(iso_to_fineract(r["payment_date"]), float(r["total_amount"]),
                                  payment_type_id=args.payment_type_id, payment_ref=utr,
                                  external_id=utr + args.external_id_suffix)
            try:
                res = client.post(f"loans/{loan['id']}/transactions?command=repayment", body)
                entry["result"] = "posted"; entry["txn_id"] = res.get("resourceId"); counts["posted"] += 1
            except FineractError as e:
                if is_duplicate_external_id(e):
                    entry["result"] = "already-posted"; counts["already-posted"] += 1
                else:
                    entry["result"] = "error"; entry["error"] = f"HTTP {e.status}: {e.body}"; counts["error"] += 1
        except FineractError as e:
            entry["result"] = "error"; entry["error"] = f"HTTP {e.status}: {e.body}"; counts["error"] += 1
        report.append(entry)
        if args.report and processed % 500 == 0 and not args.dry_run:
            import json
            json.dump(report, open(args.report, "w"), indent=2)
        if processed % 1000 == 0:
            print(f"  ...{processed} | {dict(counts)}", flush=True)

    if args.report:
        import json
        json.dump(report, open(args.report, "w"), indent=2)
    for r in report:
        if r["result"] in ("error",):
            print(f"[error] UTR {r['utr']} LMS {r['lms']} {r.get('error','')}")
    print(f"\nSTEP3 Summary ({'DRY-RUN' if args.dry_run else 'LIVE'}): {dict(counts)}")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
