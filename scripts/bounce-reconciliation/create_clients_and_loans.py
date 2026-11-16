#!/usr/bin/env python3
"""Create missing Vivriti clients + loans (pending) from the full disbursed-loan sheet.

For each sheet row: ensure the client exists (by EXTERNAL ID) creating it active if
not, then create its loan in "Submitted and pending approval" (no disbursal), using
the same product/structure as the already-migrated 97 loans. The loan's first EMI is
derived by the product's cutoff datatable (no repaymentsStartingFromDate passed).

Idempotent / resumable: the loan is keyed by externalId = LMS_ID and the client by
its EXTERNAL ID, so a re-run skips anything already created. Safe to interrupt and
restart.

Skips any LMS in --skip (default 34788,72192 -- the duplicate-client and
missing-disbursement-date anomalies) and reports them.

Usage:
  FINERACT_* env (see fineract_client.py) ...
  python3 create_clients_and_loans.py SHEET.csv \
      [--office-id 1] [--product-id 1] [--skip 34788,72192] [--limit N] [--dry-run] [--report out.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter

from fineract_client import FineractClient, FineractError

STRATEGY = "interest-principal-penalties-fees-order-strategy"


def client_exists(client: FineractClient, ext: str):
    try:
        c = client.get(f"clients/external-id/{ext}")
        return c.get("id")
    except FineractError as e:
        if e.status == 404:
            return None
        raise


def client_has_live_vivriti_loan(client: FineractClient, client_id: int) -> bool:
    """True if the client already has a non-withdrawn/non-rejected Turno Vivriti loan.

    This is the real idempotency guard: the original 97 loans were created without an
    LMS externalId, so we detect 'already migrated' by the client owning a live loan
    rather than by loan externalId (which only the loans WE create here will carry).
    """
    acc = client.get(f"clients/{client_id}/accounts") or {}
    for la in (acc.get("loanAccounts") or []):
        st = la["status"]["value"].lower()
        if la.get("productName") == "Turno Vivriti" and "withdrawn" not in st and "rejected" not in st:
            return True
    return False


def create_client(client: FineractClient, row: dict, office_id: int) -> int:
    body = {
        "officeId": office_id,
        "firstname": row["FIRST_NAME"].strip(),
        "lastname": (row["LAST_NAME"].strip() or row["FIRST_NAME"].strip()),
        "externalId": row["EXTERNAL ID"].strip(),
        "legalFormId": 1,
        "active": True,
        "activationDate": row["ACTIVATION_DATE"].strip(),
        "dateFormat": "dd MMMM yyyy",
        "locale": "en",
    }
    return client.post("clients", body)["clientId"]


def create_loan(client: FineractClient, client_id: int, row: dict, product_id: int) -> int:
    term = row["LOAN TERM FREQUENCY"].strip()
    body = {
        "clientId": client_id,
        "productId": product_id,
        "loanType": "individual",
        "principal": row["PRINCIPAL"].strip(),
        "loanTermFrequency": term,
        "loanTermFrequencyType": 2,
        "numberOfRepayments": term,
        "repaymentEvery": 1,
        "repaymentFrequencyType": 2,
        "interestRatePerPeriod": row["INTEREST RATE PER PERIOD"].strip(),
        "interestRateFrequencyType": 3,
        "amortizationType": 1,
        "interestType": 0,
        "interestCalculationPeriodType": 1,
        "transactionProcessingStrategyCode": STRATEGY,
        "expectedDisbursementDate": row["EXPECTED DISBURSEMENT DATE"].strip(),
        "submittedOnDate": row["SUBMITTED ON DATE"].strip(),
        "externalId": row["LMS_ID"].strip(),
        "dateFormat": "dd MMMM yyyy",
        "locale": "en",
    }
    return client.post("loans", body)["loanId"]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Create missing Vivriti clients + pending loans.")
    ap.add_argument("sheet")
    ap.add_argument("--office-id", type=int, default=1)
    ap.add_argument("--product-id", type=int, default=1)
    ap.add_argument("--skip", default="34788,72192", help="comma-separated LMS ids to skip")
    ap.add_argument("--limit", type=int, default=0, help="process only the first N eligible rows (0 = all)")
    ap.add_argument("--skip-done", help="prior report JSON: skip LMS already 'created'/'loan-exists' (fast resume)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", help="write per-row JSON report (rewritten as it progresses)")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}
    done = set()
    if args.skip_done:
        for x in json.load(open(args.skip_done)):
            if x.get("result") in ("created", "loan-exists"):
                done.add(x["lms"])
        print(f"resume: skipping {len(done)} LMS already done (from {args.skip_done})")
    rows = list(csv.DictReader(open(args.sheet)))

    report = []
    counts = Counter()
    processed = 0
    for r in rows:
        lms = r["LMS_ID"].strip()
        ext = r["EXTERNAL ID"].strip()
        if lms in done:
            counts["prev-done"] += 1; continue
        if lms in skip:
            report.append({"lms": lms, "result": "skipped-anomaly"}); counts["skipped"] += 1; continue
        if not r["EXPECTED DISBURSEMENT DATE"].strip() or not ext or not r["PRINCIPAL"].strip():
            report.append({"lms": lms, "result": "skipped-bad-data"}); counts["skipped"] += 1; continue

        if args.limit and processed >= args.limit:
            break
        processed += 1
        entry = {"lms": lms, "ext": ext}
        try:
            cid = client_exists(client, ext)
            entry["client_existed"] = cid is not None
            if cid is not None and client_has_live_vivriti_loan(client, cid):
                entry["result"] = "loan-exists"; counts["loan-exists"] += 1; report.append(entry); continue
            if args.dry_run:
                entry["result"] = "would-create" + ("" if cid else " (+client)"); counts["would-create"] += 1
                report.append(entry); continue
            if cid is None:
                cid = create_client(client, r, args.office_id)
            entry["client_id"] = cid
            entry["loan_id"] = create_loan(client, cid, r, args.product_id)
            entry["result"] = "created"; counts["created"] += 1
        except FineractError as e:
            entry["result"] = "error"; entry["error"] = f"HTTP {e.status}: {e.body}"; counts["error"] += 1
        report.append(entry)

        if args.report and counts["created"] % 100 == 0 and not args.dry_run:
            json.dump(report, open(args.report, "w"), indent=2)
        if processed % 200 == 0:
            print(f"  ...{processed} processed | {dict(counts)}", flush=True)

    if args.report:
        json.dump(report, open(args.report, "w"), indent=2)
    for r in report:
        if r["result"] in ("error",):
            print(f"[error] LMS {r['lms']}: {r.get('error')}")
    print(f"\nSummary ({'DRY-RUN' if args.dry_run else 'LIVE'}): {dict(counts)}")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
