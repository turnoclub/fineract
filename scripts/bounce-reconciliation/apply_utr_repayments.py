#!/usr/bin/env python3
"""Apply UTR-list repayments to their Fineract loans (idempotent via externalId=UTR).

Reads the UTR file (one payment per row) and the saved LMS->Fineract mapping, and
posts each UTR as a loan repayment:
  transactionDate = payment_date , transactionAmount = total_amount ,
  paymentTypeId   = --payment-type-id , receiptNumber = utr , externalId = utr

Because Fineract enforces uniqueness on transaction externalId, passing the UTR as
externalId makes every payment idempotent -- a re-run skips already-posted UTRs
(seen as 'externalId already exists') instead of double-posting.

Only ACTIVE loans accept repayments. Rows whose loan is still pending, missing, or
already paid (duplicate UTR) are skipped and reported -- never silently dropped.

UTR columns: utr , payment_date (YYYY-MM-DD) , total_amount , loan_ids (LMS id).

Usage:
  FINERACT_* env (see fineract_client.py) ...
  python3 apply_utr_repayments.py UTR.csv lms_fineract_mapping.csv \
      [--payment-type-id 1] [--only 25482,33096] [--dry-run] [--report out.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime

from fineract_client import FineractClient, FineractError, is_duplicate_external_id, repayment_body


def to_fineract_date(iso: str) -> str:
    return datetime.strptime(iso.strip(), "%Y-%m-%d").strftime("%d %B %Y")


def load_mapping(path: str) -> dict[str, str]:
    out = {}
    for r in csv.DictReader(open(path)):
        if r.get("FINERACT_LOAN_ID"):
            out[r["LMS_ID"].strip()] = r["FINERACT_LOAN_ID"].strip()
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Apply UTR repayments to Fineract loans (idempotent).")
    ap.add_argument("utr_csv")
    ap.add_argument("mapping_csv")
    ap.add_argument("--payment-type-id", default="1", help="paymentTypeId for the repayment (default 1)")
    ap.add_argument("--external-id-suffix", default="",
                    help="append to externalId to de-collide UTRs already locked by reversed/withdrawn "
                         "transactions (the real UTR stays in receiptNumber)")
    ap.add_argument("--only", default="", help="comma-separated LMS ids to process (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="plan only; no writes")
    ap.add_argument("--report", help="write a per-UTR JSON report")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    mapping = load_mapping(args.mapping_csv)
    only = {x.strip() for x in args.only.split(",") if x.strip()}

    rows = list(csv.DictReader(open(args.utr_csv)))
    # Apply each loan's payments in date order (stable, avoids out-of-order backdating churn).
    rows.sort(key=lambda r: (r["loan_ids"].strip(), r["payment_date"].strip()))

    status_cache: dict[str, str] = {}

    def loan_status(loan_id: str) -> str:
        if loan_id not in status_cache:
            try:
                status_cache[loan_id] = client.get(f"loans/{loan_id}")["status"]["value"]
            except FineractError as e:
                status_cache[loan_id] = f"_err{e.status}"
        return status_cache[loan_id]

    report = []
    counts = Counter()
    for r in rows:
        lms = r["loan_ids"].strip()
        utr = r["utr"].strip()
        if only and lms not in only:
            continue
        entry = {"utr": utr, "lms": lms, "amount": r["total_amount"].strip(), "date": r["payment_date"].strip()}
        loan_id = mapping.get(lms)
        if not loan_id:
            entry["result"] = "no-fineract-loan"; counts["no-loan"] += 1; report.append(entry); continue
        entry["fineract_loan_id"] = loan_id

        if args.dry_run:
            st = loan_status(loan_id)
            if st != "Active":
                entry["result"] = f"would-skip (loan {st})"; counts["skip-not-active"] += 1
            else:
                entry["result"] = "would-post"; counts["would-post"] += 1
            report.append(entry); continue

        if loan_status(loan_id) != "Active":
            entry["result"] = f"skip (loan {status_cache[loan_id]})"; counts["skip-not-active"] += 1; report.append(entry); continue

        body = repayment_body(
            to_fineract_date(r["payment_date"]),
            float(r["total_amount"]),
            payment_type_id=args.payment_type_id,
            payment_ref=utr,
            external_id=utr + args.external_id_suffix,
        )
        try:
            res = client.post(f"loans/{loan_id}/transactions?command=repayment", body)
            entry["result"] = "posted"; entry["txn_id"] = res.get("resourceId"); counts["posted"] += 1
        except FineractError as e:
            if is_duplicate_external_id(e):
                entry["result"] = "already-posted"; counts["already-posted"] += 1
            else:
                entry["result"] = "error"; entry["error"] = f"HTTP {e.status}: {e.body}"; counts["error"] += 1
        report.append(entry)

    # report
    if args.report:
        json.dump(report, open(args.report, "w"), indent=2)
    for r in report:
        if r["result"] not in ("posted", "would-post", "already-posted"):
            print(f"[{r['result']}] UTR {r['utr']} LMS {r['lms']} {r.get('error','')}")
    print(f"\nSummary ({'DRY-RUN' if args.dry_run else 'LIVE'}): {dict(counts)}")
    if args.report:
        print(f"Full per-UTR report: {args.report}")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
