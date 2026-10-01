#!/usr/bin/env python3
"""
netskope_fetch_existing.py
----------------------------------------
Fetches every existing Private App's destination host(s) from the tenant.
Used as a pre-check before creating new apps: Netskope does not create a
visible duplicate when a destination is already claimed by another app
(confirmed empirically - a POST for an already-claimed host returns HTTP 200
but no new app appears), so this lets the bash orchestrator detect and skip
those rows BEFORE attempting the POST, instead of after.

Usage:
    python3 netskope_fetch_existing.py <tenant> <token> <out_hosts_file>

Exit codes:
    0 = success - out_hosts_file written, one destination host per line, deduped
    1 = fatal error - out_hosts_file NOT written. Caller must treat this as
        "pre-check unavailable", never as "no collisions exist".

Note on pagination: Netskope's public docs don't spell out the pagination
parameters for this specific list endpoint. limit/offset is the convention
used elsewhere in Netskope's v2 API (confirmed on the audit events endpoint)
and matches a third-party API wrapper's documented schema for this exact
endpoint. If your tenant behaves differently, this script logs how many
pages/hosts it actually fetched vs. the tenant-reported total so a mismatch
is visible rather than silent.
"""
import sys
import json
import subprocess


def curl_get(url, token):
    """Runs curl exactly the way the bash orchestrator does, so behavior
    (proxy config, certs, etc.) is consistent across the whole pipeline."""
    result = subprocess.run(
        ["curl", "-s", "-w", "\n%{http_code}", "-X", "GET", url,
         "-H", f"Netskope-API-Token: {token}"],
        capture_output=True, text=True, timeout=30
    )
    stdout = result.stdout.rstrip("\n")  # bash's $(...) strips this automatically; subprocess does not
    lines = stdout.rsplit("\n", 1)
    body = lines[0] if len(lines) == 2 else ""
    code = lines[1] if len(lines) == 2 else stdout
    return code.strip(), body


def main():
    if len(sys.argv) != 4:
        print("Usage: netskope_fetch_existing.py <tenant> <token> <out_hosts_file>", file=sys.stderr)
        sys.exit(1)

    tenant, token, out_path = sys.argv[1:4]
    base_url = f"https://{tenant}.goskope.com/api/v2/steering/apps/private"

    all_hosts = set()
    offset = 0
    limit = 1000  # large enough to fetch the whole tenant in one request -
                   # offset-based multi-page pagination is confirmed NOT to work
                   # as documented conventions elsewhere suggested (page 2 with
                   # offset=100 returned empty instead of the remaining apps)
    total = None
    pages = 0
    MAX_PAGES = 20  # safety cap; with limit=1000 this should never be needed

    while True:
        url = f"{base_url}?limit={limit}&offset={offset}"
        try:
            code, body = curl_get(url, token)
        except Exception as e:
            print(f"FATAL: curl invocation failed: {e}", file=sys.stderr)
            sys.exit(1)

        if code != "200":
            print(f"FATAL: HTTP {code} fetching existing apps: {body}", file=sys.stderr)
            sys.exit(1)

        try:
            parsed = json.loads(body)
        except Exception as e:
            print(f"FATAL: could not parse response as JSON: {e}", file=sys.stderr)
            sys.exit(1)

        data = parsed.get("data", [])
        if isinstance(data, dict):
            for key in ("private_apps", "apps", "app_list"):
                if key in data:
                    data = data[key]
                    break
        if not isinstance(data, list):
            data = []

        if total is None:
            total = parsed.get("total")

        for app in data:
            host_field = app.get("host", "") or ""
            for tok in str(host_field).split(","):
                tok = tok.strip()
                if tok:
                    all_hosts.add(tok)

        pages += 1
        if len(data) == 0:
            break
        offset += limit
        if isinstance(total, int) and offset >= total:
            break
        if pages >= MAX_PAGES:
            print(f"WARNING: stopped after {MAX_PAGES} pages (safety cap) - result may be incomplete", file=sys.stderr)
            break

    with open(out_path, "w") as f:
        for h in sorted(all_hosts):
            f.write(h + "\n")

    print(f"{len(all_hosts)} unique existing destination(s) fetched across {pages} page(s) "
          f"(tenant-reported total apps: {total})", file=sys.stderr)
    if isinstance(total, int) and pages == 1 and len(all_hosts) < total:
        # can't compare host count to app count directly (one app can have many
        # hosts) but if we only ever fetched one page, flag it for visibility
        print(f"NOTE: only 1 page was fetched (limit={limit}). If this tenant "
              f"has grown beyond {limit} apps, re-check this script's limit value.", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
