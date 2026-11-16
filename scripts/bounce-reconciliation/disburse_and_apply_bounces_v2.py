#!/usr/bin/env python3
"""Disburse created Vivriti loans and apply their per-installment bounce charges.

For the BROADER set of loans created by create_clients_and_loans.py (each carries
externalId = LMS_ID), so resolution is a direct loans/external-id/{lms} lookup
(no client chain). Disbursement params come from the loan itself.

Per loan id (LMS) in the bounce file:
  1. resolve loans/external-id/{lms}; skip if 404 (loan not created yet).
  2. if "Submitted and pending approval": approve + disburse at the loan's own
     expectedDisbursementDate for its principal. Already-active -> skip (idempotent).
  3. apply each bounce row as a "Bounce Charges" (charge-id) loan charge with the
     row amount + dueDate = the installment due date; a dueDate already carrying a
     bounce is skipped, so re-runs don't duplicate.

Idempotent + resumable: safe to re-run after an interruption. Use FINERACT_THROTTLE
to stay gentle on the server.

Bounce CSV (may have a BOM / filename glued onto the first header cell -> first
column is read by position): loan id , emi_number , due date , total_bounce_to_apply.

Usage:
  FINERACT_* env (see fineract_client.py) ...
  python3 disburse_and_apply_bounces_v2.py BOUNCE.csv [--charge-id 1] [--only ...] [--limit N] [--dry-run] [--report out.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime

from fineract_client import FineractClient, FineractError

PENDING = "submitted and pending approval"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def iso_to_fineract(iso: str) -> str:
    return datetime.strptime(iso.strip(), "%Y-%m-%d").strftime("%d %B %Y")


def arr_to_fineract(a) -> str:
    return f"{a[2]:02d} {MONTHS[a[1] - 1]} {a[0]}"


def existing_bounce_due(client, loan_id, charge_id):
    out = set()
    for c in client.get(f"loans/{loan_id}/charges") or []:
        if c.get("chargeId") == charge_id and c.get("dueDate"):
            y, m, d = c["dueDate"][:3]
            out.add(f"{d:02d} {MONTHS[m - 1]} {y}")
    return out


def main(argv):
    ap = argparse.ArgumentParser(description="Disburse created loans + apply bounce charges (by externalId=LMS).")
    ap.add_argument("bounce_csv")
    ap.add_argument("--charge-id", type=int, default=1)
    ap.add_argument("--only", default="", help="comma-separated LMS ids")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    reader = csv.DictReader(open(args.bounce_csv, encoding="utf-8-sig"))
    loan_col = reader.fieldnames[0]  # tolerate filename-glued / BOM header
    by_lms = defaultdict(list)
    for r in reader:
        by_lms[r[loan_col].strip()].append(r)

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    targets = sorted(by_lms)
    if only:
        targets = [t for t in targets if t in only]

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
            loan_id = loan["id"]
            entry["loan_id"] = loan_id
            status = loan["status"]["value"]
            actions = []

            if status.lower() == PENDING:
                disb = arr_to_fineract(loan["timeline"]["expectedDisbursementDate"])
                principal = loan["principal"]
                actions.append(f"disburse@{disb} amt={principal}")
                if not args.dry_run:
                    client.post(f"loans/{loan_id}?command=approve",
                                {"approvedOnDate": disb, "dateFormat": "dd MMMM yyyy", "locale": "en"})
                    client.post(f"loans/{loan_id}?command=disburse",
                                {"actualDisbursementDate": disb, "transactionAmount": principal,
                                 "dateFormat": "dd MMMM yyyy", "locale": "en"})
            else:
                actions.append(f"already {status}")

            existing = set() if args.dry_run else existing_bounce_due(client, loan_id, args.charge_id)
            applied = 0
            for r in sorted(by_lms[lms], key=lambda x: x["due date"]):
                due = iso_to_fineract(r["due date"])
                amt = r["total_bounce_to_apply"].strip()
                if due in existing:
                    continue
                applied += 1
                if not args.dry_run:
                    client.post(f"loans/{loan_id}/charges",
                                {"chargeId": args.charge_id, "amount": float(amt), "dueDate": due,
                                 "dateFormat": "dd MMMM yyyy", "locale": "en"})
                    existing.add(due)
            actions.append(f"bounces applied={applied}/{len(by_lms[lms])}")
            entry["result"] = "ok"; entry["actions"] = actions; counts["ok"] += 1
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
    print(f"\nSummary ({'DRY-RUN' if args.dry_run else 'LIVE'}): {dict(counts)}")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
