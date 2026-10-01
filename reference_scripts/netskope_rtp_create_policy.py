#!/usr/bin/env python3
"""
netskope_rtp_create_policy.py
----------------------------------------
Reads matched_users.csv (produced by netskope_email_reconcile.py) and creates
ONE Real-time Protection Policy rule granting all matched users access to the
specified private apps.

Safety design, deliberate: the rule is ALWAYS created with enabled="0",
regardless of what's passed in. Enabling it is a separate, explicit action
(see the printed instructions at the end) - never automatic. This is a
real access grant to hundreds of real employees; the script's job is to
build and verify it exists correctly, not to flip it live unattended.

Usage:
    python3 netskope_rtp_create_policy.py <tenant> <token> <matched_users.csv> <rule_name> <group_id> <access_method> <app1,app2,...>

Example (a real request, names changed):
    python3 netskope_rtp_create_policy.py my-tenant "<token>" \\
        ./reconciliation_output/matched_users.csv "NPA-Example-Bulk-Access" 6 \\
        Client "[App-A - Client],[App-B-Client],[App-B-Client-IP]"

Example (a future, different, smaller request):
    python3 netskope_rtp_create_policy.py my-tenant "<token>" \\
        ./some_other_users.csv "NPA-Grafana-TeamX" 6 \\
        Client "[Grafana-Client]"
"""
import sys
import json
import subprocess
import csv


def curl_post(url, token, body):
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", url,
         "-H", f"Netskope-Api-Token: {token}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(body)],
        capture_output=True, text=True, timeout=90
    )
    return result.stdout


def curl_get(url, token):
    result = subprocess.run(
        ["curl", "-s", url, "-H", f"Netskope-Api-Token: {token}"],
        capture_output=True, text=True, timeout=60
    )
    return result.stdout


def main():
    if len(sys.argv) != 8:
        print("Usage: netskope_rtp_create_policy.py <tenant> <token> <matched_users.csv> <rule_name> <group_id> <access_method> <app1,app2,...>", file=sys.stderr)
        print('Example: ... "NPA-Grafana-TeamX" 6 Client "[Grafana-Client]"', file=sys.stderr)
        sys.exit(1)

    tenant, token, csv_path, rule_name, group_id, access_method_raw, apps_raw = sys.argv[1:8]
    base_url = f"https://{tenant}.goskope.com/api/v2/policy/npa/rules"

    ACCESS_METHOD = [access_method_raw]
    PRIVATE_APPS = [a.strip() for a in apps_raw.split(",") if a.strip()]

    # Sanity check: every real bracket-wrapped app name we've ever seen starts
    # with "[" and ends with "]" - catch a missing-bracket mistake now rather
    # than silently create a rule referencing an app that will never match.
    unbracketed = [a for a in PRIVATE_APPS if not (a.startswith("[") and a.endswith("]"))]
    if unbracketed:
        print(f"FATAL: these app name(s) are missing the [brackets] Netskope stores names with: {unbracketed}", file=sys.stderr)
        print("Fix the app names before proceeding - an unbracketed name will not match any real app.", file=sys.stderr)
        sys.exit(1)

    emails = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("email", "").strip()
            if email:
                emails.append(email)

    if not emails:
        print("FATAL: no emails found in the input CSV - nothing to build", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(emails)} users from {csv_path}", file=sys.stderr)
    print(f"Private apps: {PRIVATE_APPS}", file=sys.stderr)
    print(f"Access method: {ACCESS_METHOD}", file=sys.stderr)
    print(f"Target group_id: {group_id}", file=sys.stderr)
    print("", file=sys.stderr)

    payload = {
        "rule_name": rule_name,
        "enabled": "0",   # ALWAYS disabled on create - see module docstring
        "group_id": group_id,
        "policy_type": "private-app",
        "rule_data": {
            "policy_type": "private-app",
            "access_method": ACCESS_METHOD,
            "userType": "user",
            "users": emails,
            "privateApps": PRIVATE_APPS,
            "match_criteria_action": {"action_name": "allow"},
            "external_dlp": False,
            "json_version": 3,
            "show_dlp_profile_action_table": False
        }
    }

    print(f"Submitting rule creation ({len(emails)} users, disabled)...", file=sys.stderr)
    raw = curl_post(base_url, token, payload)
    try:
        parsed = json.loads(raw)
    except Exception as e:
        print(f"FATAL: could not parse create response: {e}", file=sys.stderr)
        print(raw[:1000], file=sys.stderr)
        sys.exit(1)

    if parsed.get("status") != "success":
        print("FATAL: rule creation did not return success:", file=sys.stderr)
        print(json.dumps(parsed, indent=2), file=sys.stderr)
        sys.exit(1)

    rule_id = parsed.get("data", {}).get("rule_id")
    print(f"Rule created: rule_id={rule_id} (DISABLED)", file=sys.stderr)
    print("", file=sys.stderr)

    # Verify: re-fetch the rule and confirm the user count actually matches
    print("Verifying via GET - confirming the full user list actually landed, not truncated...", file=sys.stderr)
    verify_raw = curl_get(f"{base_url}/{rule_id}", token)
    try:
        verify_parsed = json.loads(verify_raw)
    except Exception as e:
        print(f"WARNING: could not parse verification GET: {e}", file=sys.stderr)
        print(verify_raw[:1000], file=sys.stderr)
        sys.exit(1)

    stored_users = verify_parsed.get("data", {}).get("rule_data", {}).get("users", [])
    stored_apps = verify_parsed.get("data", {}).get("rule_data", {}).get("privateApps", [])

    print(f"Stored user count: {len(stored_users)} (expected {len(emails)})", file=sys.stderr)
    print(f"Stored private apps: {stored_apps}", file=sys.stderr)

    if len(stored_users) != len(emails):
        print("", file=sys.stderr)
        print("*** MISMATCH: stored user count does not match input. ***", file=sys.stderr)
        print("*** Do NOT enable this rule until this is investigated. ***", file=sys.stderr)
        sys.exit(1)

    missing = set(e.lower() for e in emails) - set(u.lower() for u in stored_users)
    if missing:
        print("", file=sys.stderr)
        print(f"*** {len(missing)} email(s) present in input but missing from stored rule: ***", file=sys.stderr)
        for m in list(missing)[:10]:
            print(f"    {m}", file=sys.stderr)
        print("*** Do NOT enable this rule until this is investigated. ***", file=sys.stderr)
        sys.exit(1)

    print("", file=sys.stderr)
    print("=== SUCCESS: rule created and verified, all users present, currently DISABLED ===", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"Rule ID: {rule_id}")
    print(f"Rule Name: {rule_name}")
    print("")
    print("This rule is intentionally DISABLED. Before enabling:")
    print("  1. Check it visually in the Netskope UI under the target group")
    print("  2. Confirm the app names and user count look right")
    print("  3. When ready, enable it explicitly:")
    print(f'     curl -s -X PATCH -H "Netskope-Api-Token: <token>" -H "Content-Type: application/json" \\')
    print(f'       "{base_url}/{rule_id}" -d \'{{"enabled": "1"}}\'')


if __name__ == "__main__":
    main()