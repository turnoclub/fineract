#!/usr/bin/env python3
"""STEP 3 (batched) - push repayments via the Fineract /batches API.

Much faster than the per-call version for large UTR files:
  - resolves loan ids from a mapping JSON (LMS -> fineract_loan_id), so NO per-loan
    resolve GETs;
  - bundles many repayments into one /batches call (round-trips cut ~BATCH_SIZE x),
    which also slashes exposure to the intermittent response-hangs;
  - non-atomic batches (enclosingTransaction=false) so an already-posted UTR in a
    batch just errors for that one row -- the rest still post (resume-safe);
  - --skip-done <report.json[,report2.json]> skips UTRs already posted/already-posted
    so a resume jumps straight to new work.

Each repayment: externalId = utr (Fineract-unique -> idempotent), receiptNumber = utr,
paymentTypeId. Only loans present in the mapping are posted; the rest are reported
as loan-not-created. Loans must already be Active (run the disburse step first).

UTR CSV (BOM tolerated): utr , payment_date (YYYY-MM-DD) , total_amount , loan_ids (LMS).

Usage:
  FINERACT_* env ...
  python3 step3_apply_repayments_batched.py UTR.csv MAPPING.json \
      [--payment-type-id 1] [--batch-size 50] [--skip-done r1.json,r2.json]
      [--external-id-suffix=-r2] [--limit N] [--dry-run] [--report out.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter

from fineract_client import FineractClient, FineractError, iso_to_fineract, repayment_body

JSON_HEADER = [{"name": "Content-Type", "value": "application/json"}]


def load_skip(paths):
    done = set()
    for p in (paths or "").split(","):
        p = p.strip()
        if not p:
            continue
        try:
            for x in json.load(open(p)):
                if x.get("result") in ("posted", "already-posted"):
                    done.add(x["utr"])
        except FileNotFoundError:
            pass
    return done


def main(argv):
    ap = argparse.ArgumentParser(description="Batched UTR repayments via /batches.")
    ap.add_argument("utr_csv")
    ap.add_argument("mapping_json", help="LMS -> {fineract_loan_id} mapping (all_loans_mapping.json)")
    ap.add_argument("--payment-type-id", default="1")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--skip-done", default="", help="comma-separated prior report json(s)")
    ap.add_argument("--external-id-suffix", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args(argv)

    client = FineractClient.from_env()
    mp = json.load(open(args.mapping_json))
    done = load_skip(args.skip_done)
    rows = list(csv.DictReader(open(args.utr_csv, encoding="utf-8-sig")))
    rows.sort(key=lambda r: (r["loan_ids"].strip(), r["payment_date"].strip()))

    report, counts = [], Counter()
    pending = []  # (utr, lms, loan_id, body_dict)
    skipped_total = 0
    for r in rows:
        utr = r["utr"].strip()
        lms = r["loan_ids"].strip()
        if utr in done:
            counts["prev-done"] += 1
            continue
        if "," in lms or ";" in lms:  # multi-loan UTR row -> can't allocate, report
            report.append({"utr": utr, "lms": lms, "result": "multi-loan-skip"}); counts["multi-loan"] += 1
            continue
        info = mp.get(lms)
        if not info:
            report.append({"utr": utr, "lms": lms, "result": "loan-not-created"}); counts["no-loan"] += 1
            continue
        body = repayment_body(iso_to_fineract(r["payment_date"]), float(r["total_amount"]),
                              payment_type_id=args.payment_type_id, payment_ref=utr,
                              external_id=utr + args.external_id_suffix)
        pending.append((utr, lms, info["fineract_loan_id"], body))
        if args.limit and len(pending) >= args.limit:
            break

    print(f"to post: {len(pending)} | skipped prev-done: {counts['prev-done']} | "
          f"no-loan: {counts['no-loan']} | multi-loan: {counts['multi-loan']}")
    if args.dry_run:
        print(f"\nSTEP3-BATCH Summary (DRY-RUN): would post {len(pending)} in "
              f"{(len(pending)+args.batch_size-1)//args.batch_size} batches")
        if args.report:
            json.dump(report, open(args.report, "w"), indent=2)
        return 0

    def flush(chunk):
        reqs = [{"requestId": i + 1,
                 "relativeUrl": f"loans/{loan_id}/transactions?command=repayment",
                 "method": "POST", "headers": JSON_HEADER, "body": json.dumps(body)}
                for i, (utr, lms, loan_id, body) in enumerate(chunk)]
        try:
            resps = client.batch(reqs, enclosing_transaction=False)
        except FineractError as e:
            for utr, lms, *_ in chunk:
                report.append({"utr": utr, "lms": lms, "result": "error", "error": f"batch {e.status}"})
                counts["error"] += 1
            return
        by_id = {rp.get("requestId"): rp for rp in resps}
        for i, (utr, lms, loan_id, _) in enumerate(chunk):
            rp = by_id.get(i + 1, {})
            sc = rp.get("statusCode")
            bodytext = json.dumps(rp.get("body", "")) if not isinstance(rp.get("body"), str) else rp.get("body", "")
            if sc and 200 <= int(sc) < 300:
                report.append({"utr": utr, "lms": lms, "result": "posted"}); counts["posted"] += 1
            elif "already exists" in bodytext or "must.be.unique" in bodytext:
                report.append({"utr": utr, "lms": lms, "result": "already-posted"}); counts["already-posted"] += 1
            else:
                report.append({"utr": utr, "lms": lms, "result": "error", "error": f"{sc}: {bodytext[:120]}"})
                counts["error"] += 1

    total = len(pending)
    for start in range(0, total, args.batch_size):
        flush(pending[start:start + args.batch_size])
        sent = min(start + args.batch_size, total)
        if args.report and (sent // args.batch_size) % 10 == 0:
            json.dump(report, open(args.report, "w"), indent=2)
        if sent % (args.batch_size * 10) == 0 or sent == total:
            print(f"  ...{sent}/{total} | {dict(counts)}", flush=True)

    if args.report:
        json.dump(report, open(args.report, "w"), indent=2)
    errs = [r for r in report if r["result"] == "error"]
    for r in errs[:10]:
        print(f"[error] UTR {r['utr']} LMS {r['lms']} {r.get('error','')}")
    print(f"\nSTEP3-BATCH Summary: {dict(counts)}")
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
