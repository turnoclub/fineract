#!/usr/bin/env python3
"""Pipeline orchestrator — runs the migration steps in order from input files.

Pass the files (in code via the CONFIG block below, or on the command line) and it
runs, in this order:
  STEP 1  create + disburse loans   (--loans)
  STEP 2  apply bounce charges       (--bounces)
  STEP 3  push repayment transactions (--utr)

Only the steps whose file is provided are run. Order matters: bounces before
repayments so each payment allocates over its bounce.

Each step is idempotent and resumable, so the whole pipeline is safe to re-run.
Reports are written next to each step. Set FINERACT_THROTTLE for a fragile server.

Usage:
  FINERACT_* env ...
  # all three:
  python3 run_pipeline.py --loans LOANS.csv --bounces BOUNCES.csv --utr UTR.csv [--dry-run]
  # just create+disburse a file of loans:
  python3 run_pipeline.py --loans LOANS.csv
  # or edit CONFIG below and run with no args.
"""
from __future__ import annotations

import argparse
import sys

import step1_create_and_disburse as step1
import step2_apply_bounces as step2
import step3_apply_repayments as step3

# Edit these to "pass files in code" and run with no CLI args.
CONFIG = {
    "loans": None,    # e.g. ".../Viviriti Disbursed Loan Data ... .csv"
    "bounces": None,  # e.g. ".../bounce_charges_... .csv"
    "utr": None,      # e.g. ".../utr_list_... .csv"
}


def main(argv):
    ap = argparse.ArgumentParser(description="Run the Fineract migration pipeline from input files.")
    ap.add_argument("--loans", default=CONFIG["loans"])
    ap.add_argument("--bounces", default=CONFIG["bounces"])
    ap.add_argument("--utr", default=CONFIG["utr"])
    ap.add_argument("--charge-id", default="1")
    ap.add_argument("--payment-type-id", default="1")
    ap.add_argument("--stop-at", choices=["active", "pending"], default="active")
    ap.add_argument("--report-prefix", default="/tmp/pipeline")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    dry = ["--dry-run"] if args.dry_run else []
    rc = 0
    if args.loans:
        print("\n========== STEP 1: create + disburse ==========")
        rc |= step1.main([args.loans, "--stop-at", args.stop_at,
                          "--report", f"{args.report_prefix}_step1.json"] + dry)
    if args.bounces:
        print("\n========== STEP 2: apply bounces ==========")
        rc |= step2.main([args.bounces, "--charge-id", str(args.charge_id), "--disburse-if-pending",
                          "--report", f"{args.report_prefix}_step2.json"] + dry)
    if args.utr:
        print("\n========== STEP 3: push repayments ==========")
        rc |= step3.main([args.utr, "--payment-type-id", str(args.payment_type_id),
                          "--report", f"{args.report_prefix}_step3.json"] + dry)
    if not (args.loans or args.bounces or args.utr):
        ap.error("provide at least one of --loans / --bounces / --utr (or set CONFIG)")
    print(f"\n========== PIPELINE DONE (rc={rc}) ==========")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
