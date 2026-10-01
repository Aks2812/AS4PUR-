#!/usr/bin/env python3
"""
netskope_reconcile.py
----------------------------------------
Produces a definitive, row-by-row account of every row read from the source
Excel file: exactly one of SUCCESS, SKIPPED_EXISTS, FAILED, VALIDATION_EXCLUDED,
or NEVER_PROCESSED. Cross-references manifest.jsonl, state.csv, and
validation_skipped.log - does not rely on the Netskope UI at all, since the
UI's alphabetical sort makes visual row-order comparison meaningless.

Usage:
    python3 netskope_reconcile.py [work_dir]
    (defaults to ./netskope_import_work)
"""
import sys
import json
import csv
import os

work_dir = sys.argv[1] if len(sys.argv) > 1 else "./netskope_import_work"
manifest_path = os.path.join(work_dir, "manifest.jsonl")
state_path = os.path.join(work_dir, "state.csv")
skipped_path = os.path.join(work_dir, "validation_skipped.log")
out_path = os.path.join(work_dir, "reconciliation_report.csv")


def main():
    if not os.path.isfile(manifest_path):
        print(f"FATAL: manifest not found at {manifest_path}", file=sys.stderr)
        sys.exit(1)

    # 1. Load manifest - the source of truth for "which rows were valid and in what order"
    manifest_rows = []
    seen_in_manifest = set()
    with open(manifest_path) as f:
        for line in f:
            obj = json.loads(line)
            row_no = obj["row_no"]
            manifest_rows.append((row_no, obj["payload"]["app_name"]))
            if row_no in seen_in_manifest:
                print(f"WARNING: row {row_no} appears MORE THAN ONCE in the manifest itself - "
                      f"this would indicate a real bug in the normalizer, not the import script", file=sys.stderr)
            seen_in_manifest.add(row_no)

    # 2. Load every attempt ever logged in state.csv
    history = {}
    if os.path.isfile(state_path):
        with open(state_path) as f:
            for line in f:
                parts = line.rstrip("\n").split(",")
                if len(parts) < 5:
                    continue
                row_no, status, ts = parts[0], parts[2], parts[4]
                history.setdefault(row_no, []).append((status, ts))

    # 3. Load rows excluded before ever reaching the manifest (validation failures)
    excluded = {}
    if os.path.isfile(skipped_path):
        with open(skipped_path) as f:
            for line in f:
                if line.startswith("Row "):
                    parts = line.split("|")
                    if len(parts) >= 3:
                        row_no = parts[0].replace("Row", "").strip()
                        reason = parts[2].strip()
                        excluded[row_no] = reason

    def final_status(row_no):
        events = history.get(str(row_no), [])
        statuses = [s for s, _ in events if s != "DRYRUN"]
        if "SUCCESS" in statuses:
            return "SUCCESS", ""
        if "SKIPPED_EXISTS" in statuses:
            return "SKIPPED_EXISTS", ""
        if statuses:
            return "FAILED", f"{len(statuses)} failed attempt(s) logged, none succeeded yet"
        return "NEVER_PROCESSED", "not yet attempted"

    counts = {"SUCCESS": 0, "SKIPPED_EXISTS": 0, "FAILED": 0,
              "NEVER_PROCESSED": 0, "VALIDATION_EXCLUDED": 0}
    rows_out = []

    # Check manifest order matches ascending row_no (proves no silent reordering)
    manifest_row_numbers = [r for r, _ in manifest_rows]
    is_ascending = all(manifest_row_numbers[i] <= manifest_row_numbers[i + 1]
                        for i in range(len(manifest_row_numbers) - 1))

    for row_no, app_name in manifest_rows:
        status, reason = final_status(row_no)
        counts[status] += 1
        rows_out.append((row_no, app_name[:60], status, reason))

    for row_no, reason in excluded.items():
        counts["VALIDATION_EXCLUDED"] += 1
        rows_out.append((row_no, "(excluded before manifest - see validation_skipped.log)",
                          "VALIDATION_EXCLUDED", reason))

    rows_out.sort(key=lambda r: int(r[0]))

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["row_no", "app_name", "final_status", "reason"])
        for r in rows_out:
            w.writerow(r)

    total = len(rows_out)
    max_row_no = max(int(r[0]) for r in rows_out) if rows_out else 0

    print(f"Reconciliation report written to: {out_path}")
    print(f"Manifest row order is strictly ascending (no reordering detected): {is_ascending}")
    print(f"Total rows accounted for: {total}")
    print(f"Highest row_no seen: {max_row_no}")
    print("")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print("")
    if total != max_row_no:
        print(f"NOTE: total rows ({total}) != highest row_no ({max_row_no}). "
              f"This is expected only if the Excel file has gaps in its 'No' column; "
              f"otherwise investigate before continuing.")

    never = [r for r in rows_out if r[2] == "NEVER_PROCESSED"]
    if never:
        first_ten = ", ".join(str(r[0]) for r in never[:10])
        more = f" (+{len(never)-10} more)" if len(never) > 10 else ""
        print(f"Rows not yet processed ({len(never)}): {first_ten}{more}")


if __name__ == "__main__":
    main()
