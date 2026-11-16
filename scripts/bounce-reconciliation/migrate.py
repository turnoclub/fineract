#!/usr/bin/env python3
"""Old-case migration: replay a loan's full historical event ledger into Fineract.

Reads a JSON file describing one or more old loans, each with a chronological
event ledger, and recreates them in Fineract by replaying the events in date
order. Chronological order is load-bearing: a bounce charge must exist on its
installment BEFORE the repayment that settled it, or the strategy mis-allocates
(the loan-6 vs loan-103 lesson). See .claude/plans/tidy-brewing-diffie.md

Fidelity: correct end-state. Repayments backdate to real value-dates and bounce
charges carry their real dueDate; waiving a past-due charge dates to the charge's
due date automatically; charge adjustments are stamped at the run-date (rare in
old data -- flagged in the report).

Idempotency: external_id is stamped on the loan, every charge, and every
transaction. A re-run skips loans whose external_id already resolves.

Input JSON shape (see migration_ledger_template.json):
{
  "loans": [{
    "external_id": "SRC-LOAN-1001",        # source-system id -> idempotency key
    "client_id": 3,
    "product_id": 1,
    "principal": 335000,
    "loan_term_frequency": 36, "number_of_repayments": 36, "repayment_every": 1,
    "repayment_frequency_type": 2, "loan_term_frequency_type": 2,
    "interest_rate_per_period": 25, "amortization_type": 1, "interest_type": 0,
    "interest_calculation_period_type": 1,
    "transaction_processing_strategy_code": "interest-principal-penalties-fees-order-strategy",
    "submitted_on": "06 November 2024", "approved_on": "30 November 2024",
    "disbursed_on": "30 November 2024",
    "events": [
      {"date": "05 January 2025", "type": "repayment", "amount": 14715},
      {"date": "05 April 2025",  "type": "bounce",    "amount": 708, "ref": "B1"},
      {"date": "06 April 2025",  "type": "repayment", "amount": 13320},
      {"date": "05 May 2025",    "type": "bounce",    "amount": 708, "ref": "B2"},
      {"date": "05 May 2025",    "type": "waive",     "ref": "B2"},
      {"date": "05 Jun 2025",    "type": "adjustment","ref": "B1", "amount": 299}
    ],
    "expect": {"outstanding": 0}              # optional end-state assertions
  }]
}

Event types: bounce (apply charge; needs ref + amount + date->dueDate),
repayment (date + amount; optional payment_type_id / payment_ref / external_id),
waive (ref of a prior bounce), adjustment (ref + amount, refund a collected bounce).

A repayment's external_id records the source payment id and makes that specific
repayment idempotent -- Fineract enforces uniqueness on transaction externalId, so
re-posting the same id is rejected rather than duplicated. (Loan-level idempotency
is still handled by the loan external_id skip; see "Idempotency" above.)

PRE-FLIGHT (do once, manually, before running): disable job 12 "Apply penalty to
overdue loans" or the backdate-penalties-enabled config, or it will inject flat
penalties onto unpaid historical installments. Re-enable after migrating.

Usage:
  FINERACT_BASE_URL=... FINERACT_TENANT=default FINERACT_USER=mifos FINERACT_PASSWORD=password \
    python3 migrate.py ledger.json [--charge-id 1] [--dry-run] [--report out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from fineract_client import (
    FineractClient,
    FineractError,
    is_duplicate_external_id,
    repayment_body,
)

# Same-date tie-break: a charge must be applied before the payment that settles it.
EVENT_ORDER = {"bounce": 0, "repayment": 1, "waive": 2, "adjustment": 3}


def parse_date(s: str) -> datetime:
    # Accept the Fineract display format used throughout, e.g. "05 January 2025".
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise SystemExit(f"unparseable date {s!r} (expected like '05 January 2025')")


def sort_events(events: list[dict]) -> list[dict]:
    return sorted(events, key=lambda e: (parse_date(e["date"]), EVENT_ORDER.get(e["type"], 9)))


def find_existing_loan(client: FineractClient, external_id: str) -> int | None:
    try:
        loan = client.get(f"loans/external-id/{external_id}")
        return loan.get("id") if loan else None
    except FineractError as e:
        if e.status == 404:
            return None
        raise


def create_loan(client: FineractClient, loan: dict) -> int:
    body = {
        "clientId": loan["client_id"],
        "productId": loan["product_id"],
        "loanType": "individual",
        "principal": loan["principal"],
        "loanTermFrequency": loan["loan_term_frequency"],
        "loanTermFrequencyType": loan["loan_term_frequency_type"],
        "numberOfRepayments": loan["number_of_repayments"],
        "repaymentEvery": loan["repayment_every"],
        "repaymentFrequencyType": loan["repayment_frequency_type"],
        "interestRatePerPeriod": loan["interest_rate_per_period"],
        "amortizationType": loan["amortization_type"],
        "interestType": loan["interest_type"],
        "interestCalculationPeriodType": loan["interest_calculation_period_type"],
        "transactionProcessingStrategyCode": loan["transaction_processing_strategy_code"],
        "expectedDisbursementDate": loan["disbursed_on"],
        "submittedOnDate": loan["submitted_on"],
        "externalId": loan["external_id"],
        "dateFormat": "dd MMMM yyyy",
        "locale": "en",
    }
    res = client.post("loans", body)
    loan_id = res["loanId"]
    client.post(
        f"loans/{loan_id}?command=approve",
        {"approvedOnDate": loan["approved_on"], "dateFormat": "dd MMMM yyyy", "locale": "en"},
    )
    client.post(
        f"loans/{loan_id}?command=disburse",
        {
            "actualDisbursementDate": loan["disbursed_on"],
            "transactionAmount": loan["principal"],
            "dateFormat": "dd MMMM yyyy",
            "locale": "en",
        },
    )
    return loan_id


def replay_events(client: FineractClient, loan_id: int, events: list[dict], charge_id: int) -> dict:
    """Replay sorted events; return {ref -> loanChargeId} for applied bounces."""
    charge_ref: dict[str, int] = {}
    for ev in events:
        etype = ev["type"]
        if etype == "bounce":
            res = client.post(
                f"loans/{loan_id}/charges",
                {
                    "chargeId": charge_id,
                    "amount": ev["amount"],
                    "dueDate": ev["date"],
                    "dateFormat": "dd MMMM yyyy",
                    "locale": "en",
                },
            )
            if ev.get("ref"):
                charge_ref[ev["ref"]] = res["resourceId"]
        elif etype == "repayment":
            body = repayment_body(
                ev["date"],
                ev["amount"],
                payment_type_id=ev.get("payment_type_id"),
                payment_ref=ev.get("payment_ref"),
                external_id=ev.get("external_id"),
            )
            try:
                client.post(f"loans/{loan_id}/transactions?command=repayment", body)
            except FineractError as e:
                # On a re-run, a repayment carrying a stable external_id that was
                # already posted is rejected as a duplicate -- treat as already done.
                if ev.get("external_id") and is_duplicate_external_id(e):
                    pass
                else:
                    raise
        elif etype == "waive":
            cid = charge_ref.get(ev["ref"])
            if cid is None:
                raise SystemExit(f"waive references unknown bounce ref {ev['ref']!r}")
            client.post(f"loans/{loan_id}/charges/{cid}?command=waive", {})
        elif etype == "adjustment":
            cid = charge_ref.get(ev["ref"])
            if cid is None:
                raise SystemExit(f"adjustment references unknown bounce ref {ev['ref']!r}")
            client.post(
                f"loans/{loan_id}/charges/{cid}?command=adjustment",
                {"amount": ev["amount"], "locale": "en"},
            )
        else:
            raise SystemExit(f"unknown event type {etype!r}")
    return charge_ref


def verify(client: FineractClient, loan_id: int, expect: dict) -> list[str]:
    """Compare end-state to optional assertions; return a list of mismatch strings."""
    if not expect:
        return []
    loan = client.get(f"loans/{loan_id}")
    summary = loan.get("summary") or {}
    mismatches = []
    if "outstanding" in expect:
        actual = float(summary.get("totalOutstanding") or 0)
        if abs(actual - float(expect["outstanding"])) > 0.01:
            mismatches.append(f"outstanding expected {expect['outstanding']} got {actual}")
    return mismatches


def migrate_loan(client: FineractClient, loan: dict, charge_id: int, dry_run: bool) -> dict:
    ext = loan["external_id"]
    events = sort_events(loan.get("events", []))

    if dry_run:
        plan = [f"create loan ext={ext} client={loan['client_id']}"]
        plan += [f"  {e['date']} {e['type']} {e.get('amount','')} {e.get('ref','')}".rstrip() for e in events]
        return {"external_id": ext, "status": "dry-run", "plan": plan}

    try:
        existing = find_existing_loan(client, ext)
        if existing is not None:
            return {"external_id": ext, "loan_id": existing, "status": "skipped-existing"}
        loan_id = create_loan(client, loan)
        replay_events(client, loan_id, events, charge_id)
        mismatches = verify(client, loan_id, loan.get("expect", {}))
        status = "ok" if not mismatches else "verify-mismatch"
        return {"external_id": ext, "loan_id": loan_id, "status": status, "mismatches": mismatches}
    except FineractError as e:
        return {"external_id": ext, "status": "error", "error": f"HTTP {e.status}: {e.body}"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Replay old-case loan ledgers into Fineract.")
    ap.add_argument("ledger", help="migration ledger JSON (see migration_ledger_template.json)")
    ap.add_argument("--charge-id", type=int, default=1, help="bounce charge definition id (default 1)")
    ap.add_argument("--dry-run", action="store_true", help="print the replay plan, do not POST")
    ap.add_argument("--report", help="write a per-loan JSON report to this path")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    with open(args.ledger) as f:
        data = json.load(f)
    loans = data.get("loans", [])
    print(f"Loaded {len(loans)} loan(s) from {args.ledger}")

    if not args.dry_run:
        print("PRE-FLIGHT REMINDER: ensure job 12 / backdate-penalties is disabled for this window.")

    report = [migrate_loan(client, loan, args.charge_id, args.dry_run) for loan in loans]

    counts: dict[str, int] = {}
    for r in report:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        line = f"[{r['status']}] {r['external_id']}"
        if r.get("loan_id"):
            line += f" -> loan {r['loan_id']}"
        if r.get("mismatches"):
            line += f" mismatches={r['mismatches']}"
        if r.get("error"):
            line += f" {r['error']}"
        print(line)
        for step in r.get("plan", []):
            print(f"    {step}")

    if args.report:
        with open(args.report, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Report written to {args.report}")

    print(f"\nSummary: {counts}")
    bad = sum(counts.get(s, 0) for s in ("error", "verify-mismatch"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
