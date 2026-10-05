#!/usr/bin/env python3
"""
netskope_xlsx_normalize.py
----------------------------------------
Reads the Private Apps Excel file, validates and normalizes each row,
and emits one ready-to-POST Netskope API v2 JSON payload per line (JSONL).

This script does NOT call the Netskope API. It only prepares data.
The bash orchestrator (netskope_bulk_import.sh) consumes its output.

Usage:
    python3 netskope_xlsx_normalize.py <input.xlsx> <publisher_ids_csv> <out_manifest.jsonl> <out_skipped.log>

    <publisher_ids_csv> is one or more publisher_id values, comma-separated,
    e.g. "1001" or "1001,1002" — every value listed is assigned to every app.

Exit codes:
    0 = completed (check out_skipped.log for any rows that were excluded)
    1 = fatal error (bad file, missing columns, etc.) - nothing was written
"""

import sys
import json
import re
import ipaddress

MAX_APP_NAME_LEN = 120           # empirically confirmed via production evidence: 120 chars succeeds,
                                  # 157 chars gets rejected by Netskope with "Private app name exceeds
                                  # character limit" - exact cutoff between 121-156 unconfirmed, using
                                  # the proven-safe value rather than guessing further into that gap
MAX_HOSTS_PER_APP = 500          # documented Netskope limit: "Up to 500 hosts can be added per client app"

# ---- Destination pattern matchers -------------------------------------------------
RE_CIDR = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}$')
RE_PLAIN_IP = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')
RE_FULL_RANGE = re.compile(r'^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})-(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$')
RE_SHORT_RANGE = re.compile(r'^(\d{1,3}\.\d{1,3}\.\d{1,3})\.(\d{1,3})-(\d{1,3})$')
RE_FQDN = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$')
RE_LABELED_HOST = re.compile(r'^([A-Za-z][A-Za-z0-9_]*)-(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?)$')
RE_PORT_TOKEN = re.compile(r'^(tcp|udp)/(\d{1,5})(-(\d{1,5}))?$', re.IGNORECASE)


def is_valid_ipv4(s):
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def expand_range(start_ip, end_ip, warnings, row_no):
    """Expand a full start-to-end IPv4 range into individual addresses."""
    try:
        start_int = int(ipaddress.IPv4Address(start_ip))
        end_int = int(ipaddress.IPv4Address(end_ip))
    except ValueError:
        warnings.append(f"Row {row_no}: invalid IP range '{start_ip}-{end_ip}', skipped this token")
        return []
    if start_int > end_int:
        warnings.append(f"Row {row_no}: range '{start_ip}-{end_ip}' has start > end, skipped this token")
        return []
    if (end_int - start_int) > 1000:
        warnings.append(f"Row {row_no}: range '{start_ip}-{end_ip}' expands to over 1000 addresses - looks like a data error, skipped this token")
        return []
    return [str(ipaddress.IPv4Address(i)) for i in range(start_int, end_int + 1)]


def normalize_host_token(tok, warnings, row_no):
    """Return a list of normalized destination strings for one comma-separated token."""
    tok = tok.strip()
    if not tok:
        return []

    # Same non-standard-dash defense as normalize_ports() - applied here too
    # in case an en-dash ever shows up in an IP range, not just a port range.
    original_tok = tok
    tok = tok.replace('\u2013', '-').replace('\u2014', '-').replace('\u2212', '-')
    if tok != original_tok:
        warnings.append(f"Row {row_no}: host token '{original_tok}' used a non-standard dash character, normalized to '{tok}'")

    # CIDR - pass through as-is (Netskope natively supports this format)
    if RE_CIDR.match(tok):
        octets_ok = all(0 <= int(o) <= 255 for o in tok.split('/')[0].split('.'))
        prefix_ok = 0 <= int(tok.split('/')[1]) <= 32
        if octets_ok and prefix_ok:
            return [tok]
        warnings.append(f"Row {row_no}: malformed CIDR '{tok}', skipped this token")
        return []

    # Full dotted-to-dotted range, e.g. 192.0.2.151-192.0.2.152
    m = RE_FULL_RANGE.match(tok)
    if m:
        return expand_range(m.group(1), m.group(2), warnings, row_no)

    # Shorthand last-octet range, e.g. 198.51.100.11-12
    m = RE_SHORT_RANGE.match(tok)
    if m:
        prefix, start_oct, end_oct = m.group(1), m.group(2), m.group(3)
        start_ip = f"{prefix}.{start_oct}"
        end_ip = f"{prefix}.{end_oct}"
        return expand_range(start_ip, end_ip, warnings, row_no)

    # Plain IPv4
    if RE_PLAIN_IP.match(tok):
        if is_valid_ipv4(tok):
            if tok.endswith('.0'):
                warnings.append(f"Row {row_no}: host '{tok}' ends in .0 with no CIDR suffix - verify this isn't a truncated /24 entry before running")
            return [tok]
        warnings.append(f"Row {row_no}: '{tok}' looks like an IP but has an out-of-range octet, skipped this token")
        return []

    # Labeled host, e.g. "LABEL-203.0.113.12/32" - a text label prefixed onto
    # an IP/CIDR. Must be checked BEFORE the FQDN fallback below: a labeled
    # host with no CIDR suffix (e.g. "LABEL-203.0.113.12") would otherwise
    # incorrectly match the FQDN pattern and pass through with the label
    # still attached, which Netskope would not accept as a valid destination.
    m = RE_LABELED_HOST.match(tok)
    if m:
        label, ip_or_cidr = m.group(1), m.group(2)
        # Re-validate the extracted portion the same way a plain IP/CIDR would be
        if '/' in ip_or_cidr:
            octets_ok = all(0 <= int(o) <= 255 for o in ip_or_cidr.split('/')[0].split('.'))
            prefix_ok = 0 <= int(ip_or_cidr.split('/')[1]) <= 32
            if not (octets_ok and prefix_ok):
                warnings.append(f"Row {row_no}: labeled host '{tok}' has a malformed CIDR after stripping label '{label}-', skipped this token")
                return []
        elif not is_valid_ipv4(ip_or_cidr):
            warnings.append(f"Row {row_no}: labeled host '{tok}' has an invalid IP after stripping label '{label}-', skipped this token")
            return []
        warnings.append(f"Row {row_no}: stripped label '{label}-' from host '{tok}', using '{ip_or_cidr}' - verify this is the intended destination")
        return [ip_or_cidr]

    # FQDN fallback - must contain a letter (avoids silently accepting garbage numeric strings)
    if RE_FQDN.match(tok) and any(c.isalpha() for c in tok):
        return [tok]

    warnings.append(f"Row {row_no}: unrecognized destination format '{tok}', skipped this token")
    return []


def normalize_hosts(cell_value, warnings, row_no):
    if not cell_value or not str(cell_value).strip():
        return []
    out = []
    for tok in str(cell_value).split(','):
        out.extend(normalize_host_token(tok, warnings, row_no))
    # de-duplicate while preserving order
    seen = set()
    deduped = []
    for h in out:
        if h not in seen:
            seen.add(h)
            deduped.append(h)
    return deduped


def normalize_ports(cell_value, warnings, row_no):
    """Ports/port ranges are passed through as-is - Netskope natively accepts port ranges
    (e.g. 20000-21000) in a single protocol entry, so we do not expand these."""
    if not cell_value or not str(cell_value).strip():
        return []
    protocols = []
    for tok in str(cell_value).split(','):
        tok = tok.strip()
        if not tok:
            continue
        # Normalize en-dash/em-dash/minus-sign to a standard hyphen - these are
        # visually indistinguishable from "-" in most editors/spreadsheets but
        # will NOT match a hyphen-based regex, silently dropping the whole
        # range (confirmed in production data: "tcp/8021–8026" survived a
        # manual cleaning pass undetected because it looks identical to "-").
        original_tok = tok
        tok = tok.replace('\u2013', '-').replace('\u2014', '-').replace('\u2212', '-')
        if tok != original_tok:
            warnings.append(f"Row {row_no}: port token '{original_tok}' used a non-standard dash character, normalized to '{tok}'")
        m = RE_PORT_TOKEN.match(tok)
        if not m:
            warnings.append(f"Row {row_no}: unrecognized port token '{tok}', skipped this token")
            continue
        proto = m.group(1).lower()
        port = m.group(2) + (m.group(3) if m.group(3) else '')
        protocols.append({"type": proto, "port": port})
    return protocols


def sanitize_app_name(name, row_no, warnings):
    name = re.sub(r'\s+', ' ', str(name).strip())
    if len(name) > MAX_APP_NAME_LEN:
        suffix = f" [Row{row_no}]"
        keep = MAX_APP_NAME_LEN - len(suffix)
        truncated = name[:keep].rstrip() + suffix
        warnings.append(f"Row {row_no}: app_name truncated from {len(name)} to {len(truncated)} chars (verify Netskope's actual limit during test batch)")
        return truncated
    return name


def main():
    if len(sys.argv) != 5:
        print("Usage: netskope_xlsx_normalize.py <input.xlsx> <publisher_id> <out_manifest.jsonl> <out_skipped.log>", file=sys.stderr)
        sys.exit(1)

    xlsx_path, publisher_ids_csv, out_manifest, out_skipped = sys.argv[1:5]

    publisher_id_list = [p.strip() for p in publisher_ids_csv.split(',') if p.strip()]
    if not publisher_id_list:
        print("FATAL: no publisher_id values provided (arg 2 was empty)", file=sys.stderr)
        sys.exit(1)

    try:
        import openpyxl
    except ImportError:
        print("FATAL: openpyxl is required (pip install openpyxl --break-system-packages)", file=sys.stderr)
        sys.exit(1)

    try:
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        ws = wb.active
    except Exception as e:
        print(f"FATAL: could not open {xlsx_path}: {e}", file=sys.stderr)
        sys.exit(1)

    headers = [c.value for c in ws[1]]
    required = ['No', 'App Name', 'Hosts (IP/CIDR)', 'Ports (Protocol/Port)']
    missing = [h for h in required if h not in headers]
    if missing:
        print(f"FATAL: missing required column(s): {missing}. Found: {headers}", file=sys.stderr)
        sys.exit(1)

    col = {h: headers.index(h) for h in headers if h is not None}

    total = 0
    written = 0
    skipped_rows = []
    warnings = []

    with open(out_manifest, 'w') as manifest_f:
        for row in ws.iter_rows(min_row=2, values_only=True):
            total += 1
            row_no = row[col['No']]
            raw_name = row[col['App Name']]
            raw_hosts = row[col['Hosts (IP/CIDR)']]
            raw_ports = row[col['Ports (Protocol/Port)']]

            if row_no is None:
                continue  # blank trailing row

            row_warnings = []
            hosts = normalize_hosts(raw_hosts, row_warnings, row_no)
            protocols = normalize_ports(raw_ports, row_warnings, row_no)
            app_name = sanitize_app_name(raw_name if raw_name else f"Unnamed_Row{row_no}", row_no, row_warnings)

            reasons = []
            if not app_name.strip():
                reasons.append("empty app_name after sanitization")
            if not hosts:
                reasons.append("no valid destination host after normalization")
            if len(hosts) > MAX_HOSTS_PER_APP:
                reasons.append(f"{len(hosts)} hosts exceeds Netskope's {MAX_HOSTS_PER_APP}-host limit per app")
            if not protocols:
                reasons.append("no valid protocol/port after normalization")

            warnings.extend(row_warnings)

            if reasons:
                skipped_rows.append((row_no, app_name, "; ".join(reasons)))
                continue

            payload = {
                "app_name": app_name,
                "host": ",".join(hosts),
                "protocols": protocols,
                "publishers": [{"publisher_id": (int(pid) if pid.isdigit() else pid)} for pid in publisher_id_list],
                "use_publisher_dns": False,
                "clientless_access": False,
                "is_user_portal_app": False,
                "trust_self_signed_certs": True
            }
            manifest_f.write(json.dumps({"row_no": row_no, "payload": payload}) + "\n")
            written += 1

    with open(out_skipped, 'w') as skip_f:
        skip_f.write(f"=== Validation summary: {total} rows read, {written} valid, {len(skipped_rows)} skipped ===\n\n")
        if skipped_rows:
            skip_f.write("--- SKIPPED ROWS (excluded from import - fix source data and re-run to include) ---\n")
            for row_no, name, reason in skipped_rows:
                skip_f.write(f"Row {row_no} | {name[:60]} | {reason}\n")
            skip_f.write("\n")
        if warnings:
            skip_f.write("--- WARNINGS (row was still included - review before running production batch) ---\n")
            for w in warnings:
                skip_f.write(w + "\n")

    print(f"{total} rows read | {written} valid payloads written | {len(skipped_rows)} skipped | {len(warnings)} warnings", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
                   