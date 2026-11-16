#!/usr/bin/env python3
"""STEP 2 - apply per-installment bounce charges, from a bounce CSV.

Resolves each loan by externalId = LMS_ID. Applies each bounce row as a "Bounce
Charges" (charge-id) loan charge with the row amount and dueDate = the installment's
due date. A dueDate already carrying a bounce is skipped (idempotent re-runs).

By default the loan must already be Active; pass --disburse-if-pending to approve +
disburse a still-pending loan first (using its own expectedDisbursementDate/principal).

Bounce CSV (BOM / a stray prefix glued onto the first header cell are tolerated — the
loan-id column is read by position): loan id , emi_number , due date , total_bounce_to_apply.

Usage:
  FINERACT_* env ...
  python3 step2_apply_bounces.py BOUNCES.csv [--charge-id 1] [--disburse-if-pending]
      [--only LMS,LMS] [--limit N] [--dry-run] [--report out.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict

from fineract_client import (DATE_FORMAT, FineractClient, FineractError, MONTHS, arr_to_fineract,
                             iso_to_fineract, resolve_loan_by_lms)

PENDING = "submitted and pending approval"


def existing_bounce_due(client, loan_id, charge_id):
    out = set()
    for c in client.get(f"loans/{loan_id}/charges") or []:
        if c.get("chargeId") == charge_id and c.get("dueDate"):
            y, m, d = c["dueDate"][:3]
            out.add(f"{d:02d} {MONTHS[m - 1]} {y}")
    return out


def main(argv):
    ap = argparse.ArgumentParser(description="Apply bounce charges by externalId=LMS.")
    ap.add_argument("bounce_csv")
    ap.add_argument("--charge-id", type=int, default=1)
    ap.add_argument("--disburse-if-pending", action="store_true",
                    help="approve+disburse a pending loan before applying bounces")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    reader = csv.DictReader(open(args.bounce_csv, encoding="utf-8-sig"))
    loan_col = reader.fieldnames[0]  # tolerate a malformed first header cell
    by_lms = defaultdict(list)
    for r in reader:
        by_lms[r[loan_col].strip()].append(r)

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    targets = [t for t in sorted(by_lms) if not only or t in only]

    report, counts, processed = [], Counter(), 0
    for lms in targets:
        if args.limit and processed >= args.limit:
            break
        processed += 1
        entry = {"lms": lms}
        try:
            loan = resolve_loan_by_lms(client, lms)
            if loan is None:
                entry["result"] = "loan-not-created"; counts["no-loan"] += 1; report.append(entry); continue
            loan_id = loan["id"]; entry["loan_id"] = loan_id
            status = loan["status"]["value"]
            if status.lower() == PENDING:
                if not args.disburse_if_pending:
                    entry["result"] = "skip-pending"; counts["skip-pending"] += 1; report.append(entry); continue
                if not args.dry_run:
                    disb = arr_to_fineract(loan["timeline"]["expectedDisbursementDate"])
                    client.post(f"loans/{loan_id}?command=approve",
                                {"approvedOnDate": disb, "dateFormat": DATE_FORMAT, "locale": "en"})
                    client.post(f"loans/{loan_id}?command=disburse",
                                {"actualDisbursementDate": disb, "transactionAmount": loan["principal"],
                                 "dateFormat": DATE_FORMAT, "locale": "en"})
            existing = set() if args.dry_run else existing_bounce_due(client, loan_id, args.charge_id)
            applied = 0
            for r in sorted(by_lms[lms], key=lambda x: x["due date"]):
                due = iso_to_fineract(r["due date"])
                if due in existing:
                    continue
                applied += 1
                if not args.dry_run:
                    client.post(f"loans/{loan_id}/charges",
                                {"chargeId": args.charge_id, "amount": float(r["total_bounce_to_apply"]),
                                 "dueDate": due, "dateFormat": DATE_FORMAT, "locale": "en"})
                    existing.add(due)
            entry["result"] = "ok"; entry["bounces_applied"] = applied; entry["bounces_total"] = len(by_lms[lms])
            counts["ok"] += 1; counts["bounces"] += applied
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
        if r["result"] in ("error", "loan-not-created"):
            print(f"[{r['result']}] LMS {r['lms']} {r.get('error','')}")
    print(f"\nSTEP2 Summary ({'DRY-RUN' if args.dry_run else 'LIVE'}): {dict(counts)}")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
