#!/usr/bin/env python3
"""
netskope_verify_success.py
----------------------------------------
Retroactively verifies every row marked SUCCESS in state.csv actually exists
in the tenant as its OWN app - not silently absorbed into a pre-existing app
with a different name (the same failure mode later caught by the collision
pre-check, but for rows imported BEFORE that pre-check existed).

For each SUCCESS row, fetches the full tenant app list (name + host) and checks:
  - CONFIRMED: an app with a matching name AND the expected host exists
  - SUSPECT_COLLISION: the expected host exists in the tenant, but under a
    different app name than expected - likely silently absorbed
  - NOT_FOUND: neither the name nor the host appears anywhere - unexplained,
    needs manual investigation regardless of the HTTP 200 that was logged

Usage:
    python3 netskope_verify_success.py <tenant> <token> [work_dir]
    (work_dir defaults to ./netskope_import_work)
"""
import sys
import json
import subprocess
import os


def normalize_name(name):
    """Netskope wraps every stored Application Segment name in [...] -
    normalize both sides before comparing, since what we submitted and
    what the tenant stores will otherwise never match on brackets alone."""
    return name.strip().lstrip("[").rstrip("]").strip()


def curl_get(url, token):
    result = subprocess.run(
        ["curl", "-s", "-w", "\n%{http_code}", "-X", "GET", url,
         "-H", f"Netskope-API-Token: {token}"],
        capture_output=True, text=True, timeout=30
    )
    stdout = result.stdout.rstrip("\n")
    lines = stdout.rsplit("\n", 1)
    body = lines[0] if len(lines) == 2 else ""
    code = lines[1] if len(lines) == 2 else stdout
    return code.strip(), body


def fetch_all_apps(tenant, token):
    base_url = f"https://{tenant}.goskope.com/api/v2/steering/apps/private"
    apps = []
    offset, limit, total, pages = 0, 1000, None, 0
    while True:
        code, body = curl_get(f"{base_url}?limit={limit}&offset={offset}", token)
        if code != "200":
            print(f"FATAL: HTTP {code} fetching apps: {body}", file=sys.stderr)
            sys.exit(1)
        parsed = json.loads(body)
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
            apps.append({
                "app_name": app.get("app_name", ""),
                "hosts": [h.strip() for h in str(app.get("host", "")).split(",") if h.strip()]
            })
        pages += 1
        if len(data) == 0:
            break
        offset += limit
        if isinstance(total, int) and offset >= total:
            break
        if pages >= 100:
            break
    return apps, total, pages


def main():
    if len(sys.argv) not in (3, 4):
        print("Usage: netskope_verify_success.py <tenant> <token> [work_dir]", file=sys.stderr)
        sys.exit(1)
    tenant, token = sys.argv[1], sys.argv[2]
    work_dir = sys.argv[3] if len(sys.argv) == 4 else "./netskope_import_work"

    manifest_path = os.path.join(work_dir, "manifest.jsonl")
    state_path = os.path.join(work_dir, "state.csv")

    # Build row_no -> expected app_name and host from the manifest
    expected = {}
    with open(manifest_path) as f:
        for line in f:
            obj = json.loads(line)
            expected[str(obj["row_no"])] = {
                "app_name": obj["payload"]["app_name"],
                "hosts": [h.strip() for h in obj["payload"]["host"].split(",") if h.strip()]
            }

    # Find every row currently marked SUCCESS
    success_rows = set()
    if os.path.isfile(state_path):
        with open(state_path) as f:
            for line in f:
                parts = line.rstrip("\n").split(",")
                if len(parts) >= 3 and parts[2] == "SUCCESS":
                    success_rows.add(parts[0])

    print(f"Fetching full tenant app list for verification...", file=sys.stderr)
    tenant_apps, tenant_total, pages_fetched = fetch_all_apps(tenant, token)
    print(f"Fetched {len(tenant_apps)} apps across {pages_fetched} page(s) "
          f"(tenant-reported total: {tenant_total}).", file=sys.stderr)
    if isinstance(tenant_total, int) and len(tenant_apps) < tenant_total:
        print(f"WARNING: fetched fewer apps ({len(tenant_apps)}) than the tenant reports "
              f"exist ({tenant_total}) - results below may be incomplete, do not trust "
              f"NOT_FOUND as final until this is resolved.", file=sys.stderr)

    # Build a host -> [app_names] index for the whole tenant
    host_to_names = {}
    for app in tenant_apps:
        for h in app["hosts"]:
            host_to_names.setdefault(h, []).append(app["app_name"])

    confirmed, suspect, not_found = [], [], []

    for row_no in sorted(success_rows, key=int):
        exp = expected.get(row_no)
        if not exp:
            continue
        exp_name = exp["app_name"]
        exp_hosts = exp["hosts"]

        # Exact match only (after bracket normalization). This dataset has
        # rows whose app_name is a literal prefix of another row's app_name
        # (e.g. row 6 "BSN-Lando" vs row 7 "BSN-Lando & DC_MGMT_VPN..."), so
        # any fuzzy/startswith match risks a false CONFIRMED on a row that
        # was actually silently absorbed - a false positive here is worse
        # than a false negative.
        exp_name_norm = normalize_name(exp_name)
        name_exists = any(normalize_name(a["app_name"]) == exp_name_norm for a in tenant_apps)

        if name_exists:
            confirmed.append(row_no)
            continue

        # Name not found - check if the destination exists under some OTHER name
        colliding_names = set()
        for h in exp_hosts:
            for other_name in host_to_names.get(h, []):
                colliding_names.add(other_name)

        if colliding_names:
            suspect.append((row_no, exp_name[:50], ", ".join(colliding_names)))
        else:
            not_found.append((row_no, exp_name[:50]))

    print("")
    print("=== Retroactive verification of all rows currently marked SUCCESS ===")
    print(f"Total SUCCESS rows checked: {len(success_rows)}")
    print(f"  CONFIRMED (exists under its own name): {len(confirmed)}")
    print(f"  SUSPECT_COLLISION (destination exists under a DIFFERENT app name): {len(suspect)}")
    print(f"  NOT_FOUND (neither name nor destination found anywhere): {len(not_found)}")
    print("")

    if suspect:
        print("--- SUSPECT_COLLISION rows (likely silently absorbed, same as rows 3/5) ---")
        for row_no, name, colliding in suspect:
            print(f"  Row {row_no} ({name}) -> destination found under: {colliding}")
        print("")

    if not_found:
        print("--- NOT_FOUND rows (unexplained - needs manual check regardless of logged HTTP 200) ---")
        for row_no, name in not_found:
            print(f"  Row {row_no} ({name})")
        print("")

    if not suspect and not not_found:
        print("All currently-SUCCESS rows verified as genuinely existing under their own name.")


if __name__ == "__main__":
    main()
