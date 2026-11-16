#!/usr/bin/env python3
"""Monthly bounce-charge reconciliation driver (three-pass, native Batch API).

Reads a reconciliation CSV and applies each loan's outcome to its open bounce
charge via Fineract's /batches endpoint, in three ordered passes:

  Pass A  repayments     -- money received this month (partial / full_collect rows)
  Pass B  waives         -- forgive the outstanding remainder (full_waive / partial / uncollect)
  Pass C  adjustments    -- refund the already-collected portion (uncollect only)

The ordering is load-bearing: a charge-adjustment credit is re-allocated by the
repayment strategy to the earliest unpaid due, so Pass B must clear the remainder
BEFORE Pass C refunds the collected amount -- only then does it roll to next month.
See the runbook: .claude/plans/tidy-brewing-diffie.md

CSV columns:
  loan_id         Fineract loan id (active/disbursed)
  outcome         full_waive | partial | full_collect | uncollect
  amount_paid     repayment amount for this month (partial / full_collect); blank otherwise
  payment_date    'dd MMMM yyyy' for the repayment; blank if no repayment
  payment_type_id (optional) paymentTypeId, e.g. the NACH/bank payment method
  payment_ref     (optional) receiptNumber -- the NACH/UTR reference, recorded on the txn
  external_id     (optional) the payment id; Fineract enforces uniqueness on it, so a
                  re-run with the same external_id is rejected instead of double-posting

The uncollect refund amount is NOT taken from the CSV -- it is read live from the
charge's current amountPaid, so it is always exactly what was collected.

Usage:
  FINERACT_BASE_URL=... FINERACT_TENANT=default FINERACT_USER=mifos FINERACT_PASSWORD=password \
    python3 reconcile.py reconciliation.csv [--charge-name "Bounce Charges"] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

from fineract_client import FineractClient, FineractError, find_bounce_charge, repayment_body

VALID_OUTCOMES = {"full_waive", "partial", "full_collect", "uncollect"}
JSON_HEADER = [{"name": "Content-Type", "value": "application/json"}]


def load_rows(csv_path: str) -> list[dict]:
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    for i, r in enumerate(rows, start=2):  # row 1 is the header
        outcome = (r.get("outcome") or "").strip()
        if outcome not in VALID_OUTCOMES:
            raise SystemExit(f"{csv_path}:{i}: invalid outcome {outcome!r} (allowed: {sorted(VALID_OUTCOMES)})")
        if not (r.get("loan_id") or "").strip():
            raise SystemExit(f"{csv_path}:{i}: missing loan_id")
    return rows


def build_passes(client: FineractClient, rows: list[dict], charge_name: str):
    """Resolve each loan's bounce charge and emit the three batch request lists.

    Returns (pass_a, pass_b, pass_c, skipped) where each pass is a list of batch
    request dicts and skipped is a list of (loan_id, reason).
    """
    pass_a, pass_b, pass_c, skipped = [], [], [], []
    rid = {"a": 0, "b": 0, "c": 0}

    def next_id(p: str) -> int:
        rid[p] += 1
        return rid[p]

    for r in rows:
        loan_id = r["loan_id"].strip()
        outcome = r["outcome"].strip()

        # Pass A: post the repayment for rows that received money this month.
        if outcome in ("partial", "full_collect"):
            amount = (r.get("amount_paid") or "").strip()
            date = (r.get("payment_date") or "").strip()
            if not amount or not date:
                skipped.append((loan_id, f"{outcome} needs amount_paid and payment_date"))
                continue
            body = repayment_body(
                date,
                float(amount),
                payment_type_id=(r.get("payment_type_id") or "").strip(),
                payment_ref=(r.get("payment_ref") or "").strip(),
                external_id=(r.get("external_id") or "").strip(),
            )
            pass_a.append({
                "requestId": next_id("a"),
                "relativeUrl": f"loans/{loan_id}/transactions?command=repayment",
                "method": "POST",
                "headers": JSON_HEADER,
                "body": json.dumps(body),
            })

        if outcome == "full_collect":
            continue  # nothing left to waive/adjust

        # Resolve the open bounce charge (needed for waive and/or adjustment).
        try:
            charge = find_bounce_charge(client, int(loan_id), charge_name)
        except FineractError as e:
            skipped.append((loan_id, f"charge lookup failed: HTTP {e.status}"))
            continue
        if charge is None:
            skipped.append((loan_id, "no open bounce charge found"))
            continue
        charge_id = charge["id"]

        # Pass B: waive the outstanding remainder (uniform across outcomes).
        pass_b.append({
            "requestId": next_id("b"),
            "relativeUrl": f"loans/{loan_id}/charges/{charge_id}?command=waive",
            "method": "POST",
            "headers": JSON_HEADER,
            "body": "{}",
        })

        # Pass C: refund the already-collected portion (uncollect only).
        if outcome == "uncollect":
            collected = charge.get("amountPaid") or 0
            if float(collected) <= 0:
                skipped.append((loan_id, "uncollect but nothing collected to refund"))
                continue
            pass_c.append({
                "requestId": next_id("c"),
                "relativeUrl": f"loans/{loan_id}/charges/{charge_id}?command=adjustment",
                "method": "POST",
                "headers": JSON_HEADER,
                "body": f'{{"amount":{collected},"locale":"en"}}',
            })

    return pass_a, pass_b, pass_c, skipped


def run_pass(client: FineractClient, name: str, requests: list[dict], dry_run: bool) -> int:
    """Execute one batch pass non-atomically. Returns the count of failed sub-requests."""
    if not requests:
        print(f"[{name}] nothing to do")
        return 0
    print(f"[{name}] {len(requests)} request(s)")
    if dry_run:
        for req in requests:
            print(f"   DRY {req['method']} {req['relativeUrl']} {req['body']}")
        return 0
    try:
        responses = client.batch(requests, enclosing_transaction=False)
    except FineractError as e:
        print(f"   ERR batch POST failed ({e.status}: {e.body}) -- {len(requests)} request(s) not applied")
        return len(requests)
    failures = 0
    by_id = {r["requestId"]: r for r in requests}
    for resp in responses:
        status = resp.get("statusCode")
        req = by_id.get(resp.get("requestId"), {})
        if status and 200 <= int(status) < 300:
            print(f"   OK  [{resp.get('requestId')}] {req.get('relativeUrl')}")
        else:
            failures += 1
            print(f"   ERR [{resp.get('requestId')}] {req.get('relativeUrl')} -> {status}: {resp.get('body')}")
    return failures


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Monthly bounce-charge reconciliation (three-pass).")
    ap.add_argument("csv", help="reconciliation CSV (see reconciliation_template.csv)")
    ap.add_argument("--charge-name", default="Bounce Charges", help="bounce charge name to match")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent, do not POST")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    rows = load_rows(args.csv)
    print(f"Loaded {len(rows)} row(s) from {args.csv}")

    pass_a, pass_b, pass_c, skipped = build_passes(client, rows, args.charge_name)

    for loan_id, reason in skipped:
        print(f"[skip] loan {loan_id}: {reason}")

    # Strict order: A (repayments) -> B (waives) -> C (adjustments).
    failures = 0
    failures += run_pass(client, "Pass A repayments", pass_a, args.dry_run)
    failures += run_pass(client, "Pass B waives", pass_b, args.dry_run)
    failures += run_pass(client, "Pass C adjustments", pass_c, args.dry_run)

    print(f"\nDone. skipped={len(skipped)} failed_requests={failures}")
    return 1 if failures or skipped else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
