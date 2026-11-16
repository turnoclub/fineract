#!/usr/bin/env python3
"""Verify per-loan repayment SPLIT (principal/interest/fees/penalty) of every migrated
loan against the VCL collections source.

VCL side: pre-aggregated per LMS loan id in vcl_split_by_loan.json (paid rows only),
with components principal, interest, bounce, fee, other, overdue, penal, amount.
Mapping VCL -> Fineract split component:
  principal  <-> principalPaid
  interest   <-> interestPaid
  fee block  <-> feeChargesPaid      (bounce + fee + other; bounce migrated as fee charge)
  penalty    <-> penaltyChargesPaid  (overdue + penal)   [penal interest was NOT migrated]

Fineract side: GET /loans/{id}?associations=summary -> summary.*Paid.

Only loans present in BOTH the mapping and VCL are checked (others ignored, per scope).
Resumable: re-run skips loans already in --report.

Usage:
  FINERACT_* env ...
  python3 verify_split.py --report out.json [--tolerance 1.0] [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter

from fineract_client import FineractClient, FineractError

DM = "../../.run/data_migrate"


def load_mapping():
    mp = {}
    for lms, info in json.load(open(f"{DM}/all_loans_mapping.json")).items():
        mp[str(lms).strip()] = info["fineract_loan_id"]
    # add the 16 EMI<interest loans created later
    p = f"{DM}/emi16_created_mapping.csv"
    if os.path.exists(p):
        for r in csv.DictReader(open(p)):
            mp[r["LMS_ID"].strip()] = int(r["FINERACT_LOAN_ID"])
    return mp


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--tolerance", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    vcl = json.load(open(f"{DM}/vcl_split_by_loan.json"))
    mp = load_mapping()

    # resume: keep prior results for loans already done
    prior = {}
    if os.path.exists(args.report):
        for x in json.load(open(args.report)):
            prior[x["lms"]] = x

    targets = [lms for lms in mp if lms in vcl]  # present in both
    todo = [lms for lms in targets if lms not in prior]
    print(f"migrated loans: {len(mp)} | in VCL: {len(targets)} | already done: {len(prior)} | to do: {len(todo)}")

    report = list(prior.values())
    counts = Counter()
    tol = args.tolerance
    done = 0
    for lms in todo:
        if args.limit and done >= args.limit:
            break
        lid = mp[lms]
        v = vcl[lms]
        # VCL "bounce" is a penalty-type charge in Fineract -> combine all charges into one bucket
        vp = round(v["principal"], 2); vi = round(v["interest"], 2)
        vc = round(v["bounce"] + v["fee"] + v["other"] + v["overdue"] + v["penal"], 2)
        e = {"lms": lms, "loan_id": lid}
        try:
            resp = client.get(f"loans/{lid}?associations=summary")
            s = (resp or {}).get("summary")
            if not s:
                e["result"] = "error"; e["error"] = "no-summary"; counts["error"] += 1
                report.append(e); done += 1; continue
            fp = round(s.get("principalPaid", 0), 2); fi = round(s.get("interestPaid", 0), 2)
            fc = round(s.get("feeChargesPaid", 0) + s.get("penaltyChargesPaid", 0), 2)
            dp, di, dc = fp - vp, fi - vi, fc - vc
            e.update(vcl_principal=vp, fin_principal=fp, d_principal=round(dp, 2),
                     vcl_interest=vi, fin_interest=fi, d_interest=round(di, 2),
                     vcl_charges=vc, fin_charges=fc, d_charges=round(dc, 2),
                     vcl_penal_notmigrated=round(v["penal"], 2),
                     vcl_total=round(vp + vi + vc, 2), fin_total=round(fp + fi + fc, 2))
            ok = all(abs(x) <= tol for x in (dp, di, dc))
            e["result"] = "match" if ok else "split-mismatch"
            counts[e["result"]] += 1
            if not ok:
                comps = [c for c, dv in (("P", dp), ("I", di), ("C", dc)) if abs(dv) > tol]
                e["mismatch_components"] = comps
        except FineractError as ex:
            e["result"] = "error"; e["error"] = f"HTTP {ex.status}"; counts["error"] += 1
        except Exception as ex:  # never let one bad response kill the whole scan
            e["result"] = "error"; e["error"] = f"{type(ex).__name__}: {ex}"; counts["error"] += 1
        report.append(e)
        done += 1
        if done % 100 == 0:
            json.dump(report, open(args.report, "w"), indent=2)
            print(f"  ...{done}/{len(todo)} | {dict(counts)}", flush=True)

    json.dump(report, open(args.report, "w"), indent=2)
    print(f"\nVERIFY-SPLIT done this run: {dict(counts)} | total in report: {len(report)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
