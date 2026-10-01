#!/usr/bin/env python3
"""
netskope_email_reconcile.py
----------------------------------------
Pulls every member of the SCIM sync group you name from the live tenant via POST /api/v2/users/getusers
(server-side filtered - confirmed matches a customer's known member count), builds a
userName -> real email lookup, and cross-references it against the customer's Excel
file (which currently has "SAM Account Name@domain" / UPN in place of a real
email - confirmed via live UI check that these are two different fields).

Output: a corrected CSV/JSONL with real emails, PLUS an explicit report of any
input rows that found no match at all - a user in the access-request list who
doesn't resolve to a real, current account in that group is a real problem worth
surfacing before a policy gets built around them, not something to silently drop.

Usage:
    python3 netskope_email_reconcile.py <tenant> <token> <input.xlsx> <out_dir> <group>

<group> is the tenant's SCIM sync group name (tenant-specific, so there is no default).
"""
import sys
import json
import subprocess
import os
import csv

PAGE_LIMIT = 200          # confirmed working page size against the real tenant
USAGE = "Usage: netskope_email_reconcile.py <tenant> <token> <input.xlsx> <out_dir> <group>"


def curl_post(url, token, body):
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", url,
         "-H", f"Netskope-Api-Token: {token}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(body)],
        capture_output=True, text=True, timeout=60
    )
    return result.stdout


def fetch_all_group_members(tenant, token, group_name):
    """Returns dict: lowercased userName -> real email, plus (total, pages) for reporting."""
    base_url = f"https://{tenant}.goskope.com/api/v2/users/getusers"
    lookup = {}
    offset = 0
    total = None
    pages = 0
    MAX_PAGES = 30  # safety cap - 1526 users / 200 per page = ~8 pages expected

    while True:
        body = {
            "query": {
                "paging": {"offset": offset, "limit": PAGE_LIMIT},
                "filter": {
                    "and": [
                        {"accounts.parentGroups": {"eq": group_name}},
                        {"accounts.deleted": {"eq": False}}
                    ]
                }
            }
        }
        raw = curl_post(base_url, token, body)
        try:
            parsed = json.loads(raw)
        except Exception as e:
            print(f"FATAL: could not parse response at offset {offset}: {e}", file=sys.stderr)
            print(raw[:500], file=sys.stderr)
            sys.exit(1)

        if total is None:
            total = parsed.get("counts", {}).get("totalResults")

        data = parsed.get("data", [])
        for user in data:
            real_email = None
            if user.get("emails"):
                real_email = user["emails"][0]
            for acct in user.get("accounts", []):
                if acct.get("deleted"):
                    continue
                uname = (acct.get("userName") or "").strip().lower()
                if uname and real_email:
                    lookup[uname] = real_email

        pages += 1
        if len(data) == 0:
            break
        offset += PAGE_LIMIT
        if isinstance(total, int) and offset >= total:
            break
        if pages >= MAX_PAGES:
            print(f"WARNING: stopped after {MAX_PAGES} pages safety cap - result may be incomplete", file=sys.stderr)
            break

    return lookup, total, pages


def main():
    if len(sys.argv) != 6 or not sys.argv[5].strip():
        print(USAGE, file=sys.stderr)
        print("<group> is required: the SCIM sync group whose members are fetched (it differs per tenant).", file=sys.stderr)
        sys.exit(1)

    tenant, token, xlsx_path, out_dir, group = sys.argv[1:6]
    group = group.strip()
    os.makedirs(out_dir, exist_ok=True)

    try:
        import openpyxl
    except ImportError:
        print("FATAL: openpyxl required (pip install openpyxl --break-system-packages)", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching all {group} members from {tenant}...", file=sys.stderr)
    lookup, total, pages = fetch_all_group_members(tenant, token, group)
    print(f"Fetched {len(lookup)} unique accounts across {pages} page(s) "
          f"(tenant-reported total: {total})", file=sys.stderr)

    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    headers = [c.value for c in ws[1]]
    upn_col = headers.index('SAM Account Name')
    display_col = headers.index('Display Name')

    matched = []
    unmatched = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        upn = str(row[upn_col]).strip().lower() if row[upn_col] else ""
        display_name = row[display_col]
        real_email = lookup.get(upn)
        if real_email:
            matched.append({"upn": row[upn_col], "display_name": display_name, "email": real_email})
        else:
            unmatched.append({"upn": row[upn_col], "display_name": display_name})

    # Write matched (the real input for the RTP automation)
    matched_path = os.path.join(out_dir, "matched_users.csv")
    with open(matched_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["upn", "display_name", "email"])
        w.writeheader()
        w.writerows(matched)

    # Write unmatched (needs human review before anything proceeds)
    unmatched_path = os.path.join(out_dir, "unmatched_users.csv")
    with open(unmatched_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["upn", "display_name"])
        w.writeheader()
        w.writerows(unmatched)

    print("", file=sys.stderr)
    print(f"=== Reconciliation summary ===", file=sys.stderr)
    print(f"Total rows in Excel: {len(matched) + len(unmatched)}", file=sys.stderr)
    print(f"Matched (real email found): {len(matched)} -> {matched_path}", file=sys.stderr)
    print(f"UNMATCHED (no account found in {group}): {len(unmatched)} -> {unmatched_path}", file=sys.stderr)
    if unmatched:
        print("", file=sys.stderr)
        print("First unmatched rows (needs review before proceeding):", file=sys.stderr)
        for u in unmatched[:10]:
            print(f"  {u['upn']} | {u['display_name']}", file=sys.stderr)


if __name__ == "__main__":
    main()