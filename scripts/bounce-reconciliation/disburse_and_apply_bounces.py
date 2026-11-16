#!/usr/bin/env python3
"""Disburse pending Vivriti loans and apply their real per-installment bounce charges.

One-time migration for the Viviriti dataset. Joins two CSVs:
  - bounce sheet : loan id (LMS) , emi_number , due date , total_bounce_to_apply
  - mapping sheet: LMS_ID -> EXTERNAL ID (Fineract client) + disbursement params

Resolution chain: bounce.loan id == mapping.LMS_ID ; mapping.EXTERNAL ID ==
Fineract client externalId ; the client's single live "Turno Vivriti" loan is the
target (withdrawn/rejected loans are ignored).

Per target loan:
  1. if still "Submitted and pending approval" -> approve + disburse using the
     mapping sheet (principal, expected disbursement date). Already-active loans
     are left as-is (idempotent).
  2. apply each bounce row as a "Bounce Charges" (chargeId) loan charge with the
     row's amount (override) and dueDate = the installment due date. A bounce whose
     dueDate already exists on the loan is skipped, so re-runs don't duplicate.

PRE-FLIGHT (do once, manually, and confirm): disable job 12 "Apply penalty to
overdue loans" or the backdate-penalties config. Disbursing these backdated loans
makes many installments overdue immediately; the nightly job would otherwise inject
flat penalties on top of the real bounces. Re-enable when done.

Excludes any LMS id in --exclude (default: none). The 38133 loan (already active and
separately test-polluted) should be passed via --exclude and handled on its own.

Usage:
  FINERACT_* env (see fineract_client.py) ...
  python3 disburse_and_apply_bounces.py BOUNCE.csv MAPPING.csv \
      [--charge-id 1] [--exclude 38133] [--only 25135,25482] [--dry-run] [--report out.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime

from fineract_client import FineractClient, FineractError

PENDING = "submitted and pending approval"


def to_fineract_date(iso: str) -> str:
    """'2026-04-05' -> '05 April 2026' (Fineract 'dd MMMM yyyy')."""
    return datetime.strptime(iso.strip(), "%Y-%m-%d").strftime("%d %B %Y")


def is_live(status: str) -> bool:
    s = status.lower()
    return "withdrawn" not in s and "rejected" not in s


def load_inputs(bounce_csv: str, mapping_csv: str):
    bounce_rows = list(csv.DictReader(open(bounce_csv)))
    mapping = {r["LMS_ID"].strip(): r for r in csv.DictReader(open(mapping_csv))}
    bounces_by_lms: dict[str, list] = defaultdict(list)
    for r in bounce_rows:
        bounces_by_lms[r["loan id"].strip()].append(r)
    return bounces_by_lms, mapping


def resolve_loan(client: FineractClient, mapping_row: dict):
    """Return (loan_dict, None) for the client's single live Vivriti loan, or (None, reason)."""
    ext = mapping_row["EXTERNAL ID"].strip()
    try:
        cl = client.get(f"clients/external-id/{ext}")
    except FineractError as e:
        return None, f"client lookup failed ({e.status})"
    if not cl or not cl.get("id"):
        return None, f"client not found (ext={ext})"
    acc = client.get(f"clients/{cl['id']}/accounts") or {}
    loans = [
        la for la in (acc.get("loanAccounts") or [])
        if la.get("productName") == "Turno Vivriti" and is_live(la["status"]["value"])
    ]
    if len(loans) != 1:
        return None, f"{len(loans)} live Vivriti loans for client {cl['id']}"
    return loans[0], None


def ensure_disbursed(client: FineractClient, loan: dict, mapping_row: dict, dry_run: bool) -> list[str]:
    """Approve + disburse a pending loan. Returns a list of action strings taken/planned."""
    loan_id = loan["id"]
    status = loan["status"]["value"]
    if status.lower() != PENDING:
        return [f"already '{status}', skip approve/disburse"]
    disb_date = mapping_row["EXPECTED DISBURSEMENT DATE"].strip()
    principal = mapping_row["PRINCIPAL"].strip()
    actions = [f"approve@{disb_date}", f"disburse@{disb_date} amount={principal}"]
    if dry_run:
        return actions
    client.post(f"loans/{loan_id}?command=approve",
                {"approvedOnDate": disb_date, "dateFormat": "dd MMMM yyyy", "locale": "en"})
    client.post(f"loans/{loan_id}?command=disburse",
                {"actualDisbursementDate": disb_date, "transactionAmount": principal,
                 "dateFormat": "dd MMMM yyyy", "locale": "en"})
    return actions


def existing_bounce_due_dates(client: FineractClient, loan_id: int, charge_id: int) -> set[str]:
    """Set of 'dd MMMM yyyy' due dates already carrying a bounce charge on the loan."""
    charges = client.get(f"loans/{loan_id}/charges") or []
    out = set()
    for c in charges:
        if c.get("chargeId") == charge_id and c.get("dueDate"):
            y, m, d = c["dueDate"][:3]
            out.add(datetime(y, m, d).strftime("%d %B %Y"))
    return out


def apply_bounces(client: FineractClient, loan_id: int, rows: list, charge_id: int, dry_run: bool) -> list[str]:
    """Apply each bounce row idempotently (skip if a bounce already exists on that due date)."""
    actions = []
    existing = set() if dry_run else existing_bounce_due_dates(client, loan_id, charge_id)
    for r in sorted(rows, key=lambda x: x["due date"]):
        due = to_fineract_date(r["due date"])
        amount = r["total_bounce_to_apply"].strip()
        if due in existing:
            actions.append(f"skip EMI{r['emi_number']} {due} (already present)")
            continue
        actions.append(f"apply EMI{r['emi_number']} {due} amount={amount}")
        if not dry_run:
            client.post(f"loans/{loan_id}/charges",
                        {"chargeId": charge_id, "amount": float(amount), "dueDate": due,
                         "dateFormat": "dd MMMM yyyy", "locale": "en"})
            existing.add(due)
    return actions


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Disburse pending Vivriti loans and apply real bounce charges.")
    ap.add_argument("bounce_csv")
    ap.add_argument("mapping_csv")
    ap.add_argument("--charge-id", type=int, default=1, help="Bounce Charges definition id (default 1)")
    ap.add_argument("--exclude", default="", help="comma-separated LMS ids to skip (e.g. the 38133 special case)")
    ap.add_argument("--only", default="", help="comma-separated LMS ids to process (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="resolve + plan only; no writes")
    ap.add_argument("--report", help="write a per-loan JSON report")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    bounces_by_lms, mapping = load_inputs(args.bounce_csv, args.mapping_csv)
    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
    only = {x.strip() for x in args.only.split(",") if x.strip()}

    targets = sorted(bounces_by_lms)
    if only:
        targets = [t for t in targets if t in only]
    print(f"{len(targets)} LMS loan(s) to process (excluding {sorted(exclude) or 'none'})")
    if not args.dry_run:
        print("PRE-FLIGHT REMINDER: ensure job 12 / backdate-penalties is disabled for this window.")

    report = []
    for lms in targets:
        if lms in exclude:
            report.append({"lms": lms, "status": "excluded"})
            continue
        if lms not in mapping:
            report.append({"lms": lms, "status": "no-mapping-row"})
            continue
        loan, reason = resolve_loan(client, mapping[lms])
        if loan is None:
            report.append({"lms": lms, "status": "unresolved", "reason": reason})
            continue
        loan_id = loan["id"]
        entry = {"lms": lms, "fineract_loan_id": loan_id, "status": "ok", "actions": []}
        try:
            entry["actions"] += ensure_disbursed(client, loan, mapping[lms], args.dry_run)
            entry["actions"] += apply_bounces(client, loan_id, bounces_by_lms[lms], args.charge_id, args.dry_run)
        except FineractError as e:
            entry["status"] = "error"
            entry["error"] = f"HTTP {e.status}: {e.body}"
        report.append(entry)

    # summary
    from collections import Counter
    counts = Counter(r["status"] for r in report)
    for r in report:
        head = f"[{r['status']}] LMS {r['lms']}"
        if r.get("fineract_loan_id"):
            head += f" -> loan {r['fineract_loan_id']}"
        if r.get("reason"):
            head += f" ({r['reason']})"
        if r.get("error"):
            head += f" {r['error']}"
        print(head)
        for a in r.get("actions", []):
            print(f"    {a}")
    if args.report:
        json.dump(report, open(args.report, "w"), indent=2)
        print(f"Report written to {args.report}")
    print(f"\nSummary: {dict(counts)}")
    return 1 if (counts.get("error", 0) or counts.get("unresolved", 0)) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
