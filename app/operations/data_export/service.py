"""
Business logic for Data Export (fifth operation): turns what Netskope returned
into CSV files. Pure functions for the mapping (tested on real, sanitized
lab-tenant responses), plus the two blocking export runs the routes hand to a
worker thread.

Private apps CSV - one row per app. The first 11 columns are Netskope's own
export columns, with Netskope's header text, so a file can be compared with or
substituted for Netskope's export. Netskope's export has NO protocol/port
column, which is the reason this exists; the appended columns add it (and
what else the API returns that the export drops).

Port columns and re-import. Private App Import reads a "Ports (Protocol/Port)"
cell with reference_scripts/netskope_xlsx_normalize.py's normalize_ports():
comma-separated `tcp|udp/<port>` or `tcp|udp/<from>-<to>` tokens
(RE_PORT_TOKEN, line 40; the template's own example is "tcp/443, tcp/8000-8010",
app/operations/private_app_import/template_file.py line 16). tcp_ports and
udp_ports use exactly that token format, joined with ", " like the template, so
`tcp_ports + ", " + udp_ports` is a ready-to-import cell (tested against the
real parser on all 14 real apps). Port strings are copied verbatim - parsed only
to order them by numeric start port, never to expand, merge or drop. Anything
that is neither tcp nor udp goes to other_ports as "transport:port" so nothing is
lost (the importer cannot take those).

Users/Groups/Memberships CSVs - see build_users_groups_tables().

Held in memory only: see routes.py. Nothing here touches the disk.
"""
from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from collections import Counter, namedtuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ...config import settings
from ...timeutil import utcnow
from ..netskope_http import NetskopeApiError
from . import netskope_client as client

# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------

# Netskope's own export header, first 11 columns as spelled there (its 13-column
# export also has "Labels" and "Publisher", which this file replaces with the
# structured columns below). Header text is compared against Netskope's real
# export in tests.
APPS_COMPAT_HEADER = [
    "Application Segment", "Destination", "Public Host", "Access Method", "Browser Access Protocol",
    "Use Publisher DNS", "In Steering", "In Access Policy", "Private App Segment Tags", "CORS Enabled", "Last Modified",
]
APPS_EXTRA_HEADER = [
    "private_app_id", "tcp_ports", "udp_ports", "other_ports", "real_host", "custom_host", "private_app_protocol",
    "trust_self_signed_certs", "modified_by", "publisher_names", "publisher_ids", "publisher_reachability",
    "policies", "steering_configs", "tags_ids",
]
APPS_HEADER = APPS_COMPAT_HEADER + APPS_EXTRA_HEADER

USERS_HEADER = [
    "email", "user_name", "given_name", "family_name", "provisioner", "last_provisioner", "active", "deleted", "ou",
    "direct_groups", "inherited_groups", "created", "last_modified", "last_modified_by", "scim_id", "scim_external_id",
]
GROUPS_HEADER = [
    "group_name", "display_name", "scim_id", "provisioner", "last_provisioner", "deleted", "created", "last_modified",
    "last_modified_by", "member_count",
]
MEMBERSHIPS_HEADER = ["email", "user_name", "group_name", "kind", "group_known"]

CSV_CONTENT_TYPE = "text/csv; charset=utf-8"
ZIP_CONTENT_TYPE = "application/zip"

# --------------------------------------------------------------------------
# Small value helpers
# --------------------------------------------------------------------------

_INJECTION_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str:
    """CSV-injection guard: a text cell a spreadsheet would read as a formula
    (starts with = + - @ tab or CR) gets a single leading quote."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in _INJECTION_TRIGGERS else text


def build_csv_bytes(header: list[str], rows: list[list]) -> bytes:
    """UTF-8 with a BOM (so Excel opens it as UTF-8), via the csv module, every
    data cell passed through csv_safe. Built in memory."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_safe(cell) for cell in row])
    return buffer.getvalue().encode("utf-8-sig")


def build_zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def export_filename(tenant: str, kind: str, now: datetime, extension: str = "csv") -> str:
    return f"as4pur_{tenant.strip().lower()}_{kind}_{now:%Y%m%d-%H%M%S}Z.{extension}"


def _text(value) -> str:
    return "" if value is None else str(value)


def _yes_no(value) -> str:
    return "yes" if value is True else "no"


def _true_false(value) -> str:
    return "true" if value is True else "false" if value is False else ""


def iso_z_from_modify_time(value) -> str | None:
    """Netskope's `modify_time` ("2025-08-19 06:35:51": no timezone) as ISO 8601 with Z.
    The API states no timezone; UTC is INFERRED from the same API's explicit-UTC
    timestamps (protocols[].updated_at ends in Z and matches modify_time to the second
    on 10 of 14 apps) - not verified against Netskope. None if it doesn't parse."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_z_from_epoch(value) -> str:
    """A getusers/getgroups timestamp (epoch seconds, a float, absolute) as ISO 8601
    UTC, truncated to the second. Blank when there is nothing usable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    try:
        if not math.isfinite(value):
            return ""
        return datetime.fromtimestamp(math.floor(value), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


# --------------------------------------------------------------------------
# Private apps
# --------------------------------------------------------------------------

PortColumns = namedtuple("PortColumns", "tcp udp other")

_PORT_SORT = re.compile(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?")


def _port_sort_key(port_text: str):
    m = _PORT_SORT.fullmatch(port_text)
    if m:
        start = int(m.group(1))
        return (0, start, int(m.group(2)) if m.group(2) else start, port_text)
    return (1, 0, 0, port_text)


def aggregate_ports(protocols) -> PortColumns:
    """protocols[] (one entry per port or range, lowercase `transport`, `port` a string)
    -> tcp / udp / other cells. Port text is emitted exactly as received; it is parsed only
    to sort by numeric start port (unparseable ones last). See the module docstring for the
    import-compatible token format."""
    tcp: list[str] = []
    udp: list[str] = []
    other: list[tuple[str, str]] = []
    for entry in protocols if isinstance(protocols, list) else []:
        if not isinstance(entry, dict):
            continue
        transport = _text(entry.get("transport"))
        port = _text(entry.get("port"))
        kind = transport.strip().lower()
        if kind == "tcp":
            tcp.append(port)
        elif kind == "udp":
            udp.append(port)
        else:
            other.append((transport, port))
    tcp.sort(key=_port_sort_key)
    udp.sort(key=_port_sort_key)
    other.sort(key=lambda item: (item[0].lower(), _port_sort_key(item[1])))
    return PortColumns(
        tcp=", ".join(f"tcp/{p}" for p in tcp),
        udp=", ".join(f"udp/{p}" for p in udp),
        other=";".join(f"{t}:{p}" for t, p in other),
    )


def reachability_state(reach) -> str:
    """Netskope's reachability object is tri-state in practice: reachable, not reachable
    (with an error code/string), or null when it cannot check at all."""
    if isinstance(reach, dict):
        if reach.get("reachable") is True:
            return "reachable"
        if reach.get("reachable") is False:
            return "not_reachable"
    return "unknown"


@dataclass
class AppsTable:
    rows: list[list[str]]
    warnings: list[str] = field(default_factory=list)


def build_apps_table(apps: list[dict]) -> AppsTable:
    rows: list[list[str]] = []
    odd_times = 0
    for app in apps:
        clientless = app.get("clientless_access") is True
        publishers = [p for p in (app.get("service_publisher_assignments") or []) if isinstance(p, dict)]
        tags = [t for t in (app.get("tags") or []) if isinstance(t, dict)]
        policies = [p for p in (app.get("policies") or []) if isinstance(p, str)]
        steering = [s for s in (app.get("steering_configs") or []) if isinstance(s, str)]
        ports = aggregate_ports(app.get("protocols"))

        raw_time = app.get("modify_time")
        last_modified = iso_z_from_modify_time(raw_time)
        if last_modified is None:
            last_modified = _text(raw_time)
            if last_modified:
                odd_times += 1

        rows.append([
            # ---- Netskope's own columns ----
            _text(app.get("app_name")),
            _text(app.get("host")),
            _text(app.get("public_host")),
            _yes_no(clientless),
            _text(app.get("private_app_protocol")) if clientless else "",
            _yes_no(app.get("use_publisher_dns")),
            str(len(steering)),
            str(len(policies)),
            ";".join(_text(t.get("tag_name")) for t in tags),
            _yes_no(app.get("allow_unauthenticated_cors")),
            last_modified,
            # ---- appended ----
            _text(app.get("app_id")),
            ports.tcp,
            ports.udp,
            ports.other,
            _text(app.get("real_host")),
            _text(app.get("custom_host")),
            _text(app.get("private_app_protocol")),
            _true_false(app.get("trust_self_signed_certs")),
            _text(app.get("modified_by")),
            ";".join(_text(p.get("publisher_name")) for p in publishers),
            ";".join(_text(p.get("publisher_id")) for p in publishers),
            ";".join(reachability_state(p.get("reachability")) for p in publishers),
            ";".join(sorted(policies)),        # policies and steering_configs come back in an unstable order
            ";".join(sorted(steering)),
            ";".join(_text(t.get("tag_id")) for t in tags),
        ])

    warnings = []
    if odd_times:
        warnings.append(
            f"{odd_times} private app(s) have a Last Modified value in an unexpected format; "
            f"it was exported exactly as Netskope returned it."
        )
    return AppsTable(rows=rows, warnings=warnings)


# --------------------------------------------------------------------------
# Users, groups, memberships
# --------------------------------------------------------------------------

@dataclass
class UsersGroupsTables:
    users_rows: list[list[str]]
    groups_rows: list[list[str]]
    memberships_rows: list[list[str]]
    stats: dict
    warnings: list[str] = field(default_factory=list)


def _names(account: dict, key: str) -> list[str]:
    value = account.get(key)
    return [n for n in value if isinstance(n, str) and n] if isinstance(value, list) else []


def _first(value) -> str:
    return _text(value[0]) if isinstance(value, list) and value else ""


def _pick(account: dict, user: dict, key: str) -> str:
    """The account's own value, else the user's - names sit on the user and, on most
    accounts, again on the account."""
    own = account.get(key)
    return _text(own if own not in (None, "") else user.get(key))


def build_users_groups_tables(users: list[dict], groups: list[dict], include_deleted: bool) -> UsersGroupsTables:
    """One users row per ACCOUNT (a user may have several - real: two users each had a deleted AD
    account beside a live one). Live vs deleted is decided by `deleted` alone: `active` is true even on
    deleted accounts. `member_count` counts LIVE accounts that list the group in their direct groups
    (parentGroups: group NAMES, equal to getgroups `id`) regardless of the toggle; the API's own
    `userCount` is ignored (it appears on only some groups). `group_known` is checked against every
    group returned, deleted or not. Membership is read from users only - no group object lists members."""
    accounts = [(u, a) for u in users for a in (u.get("accounts") or []) if isinstance(a, dict)]
    live = [(u, a) for u, a in accounts if a.get("deleted") is not True]
    included = accounts if include_deleted else live
    known_groups = {g.get("id") for g in groups}

    member_counts: Counter = Counter()
    for _, a in live:
        for name in set(_names(a, "parentGroups")):
            member_counts[name] += 1

    users_rows: list[list[str]] = []
    memberships_rows: list[list[str]] = []
    unknown_memberships = 0
    for u, a in included:
        email = _first(a.get("emails")) or _first(u.get("emails"))
        user_name = _text(a.get("userName"))
        direct = sorted(set(_names(a, "parentGroups")))
        inherited = sorted(set(_names(a, "ancestorGroups")))
        users_rows.append([
            email, user_name, _pick(a, u, "givenName"), _pick(a, u, "familyName"),
            _text(a.get("provisioner")), _text(a.get("lastProvisioner")),
            _true_false(a.get("active")), _true_false(a.get("deleted")), _text(a.get("ou")),
            ";".join(direct), ";".join(inherited),
            iso_z_from_epoch(a.get("created")), iso_z_from_epoch(a.get("lastModified")),
            _text(a.get("lastModifiedBy")), _text(a.get("scimId")), _text(a.get("scimExternalId")),
        ])
        for kind, names in (("direct", direct), ("inherited", inherited)):
            for name in names:
                known = name in known_groups
                unknown_memberships += 0 if known else 1
                memberships_rows.append([email, user_name, name, kind, "true" if known else "false"])

    groups_rows = [
        [
            _text(g.get("id")), _text(g.get("displayName")), _text(g.get("scimId")), _text(g.get("provisioner")),
            _text(g.get("lastProvisioner")), _true_false(g.get("deleted")),
            iso_z_from_epoch(g.get("created")), iso_z_from_epoch(g.get("lastModified")), _text(g.get("lastModifiedBy")),
            str(member_counts.get(g.get("id"), 0)),
        ]
        for g in groups
        if include_deleted or g.get("deleted") is not True
    ]

    warnings = []
    if unknown_memberships:
        warnings.append(
            f"{unknown_memberships} membership row(s) name a group that is not in the groups list (group_known = false)."
        )
    stats = {
        "users": len(users),
        "accounts": len(accounts),
        "live_accounts": len(live),
        "deleted_accounts": len(accounts) - len(live),
        "groups": len(groups),
        "memberships": len(memberships_rows),
    }
    return UsersGroupsTables(users_rows, groups_rows, memberships_rows, stats, warnings)


# --------------------------------------------------------------------------
# The two runs (blocking - the routes call these from a worker thread)
# --------------------------------------------------------------------------

@dataclass
class ExportFile:
    kind: str                  # private_apps | users | groups | memberships | users_groups_zip
    filename: str
    content: bytes
    content_type: str
    rows: int | None           # data rows; None for the zip


@dataclass
class ExportResult:
    kind: str                  # private_apps | users_groups
    tenant: str
    created_at: datetime       # naive UTC, like every stored timestamp in this app
    options: dict
    summary: dict
    warnings: list[str]
    files: dict[str, ExportFile]
    traces: dict[str, list] = field(default_factory=dict)


def run_private_apps_export(tenant: str, token: str, now: datetime | None = None) -> ExportResult:
    now = now or utcnow()
    tenant = tenant.strip().lower()
    fetched = client.fetch_private_apps(tenant, token)      # raises unless len(apps) == total
    table = build_apps_table(fetched.apps)
    file = ExportFile(
        kind="private_apps",
        filename=export_filename(tenant, "private_apps", now),
        content=build_csv_bytes(APPS_HEADER, table.rows),
        content_type=CSV_CONTENT_TYPE,
        rows=len(table.rows),
    )
    return ExportResult(
        kind="private_apps", tenant=tenant, created_at=now, options={},
        summary={"apps_exported": len(table.rows), "apps_total": fetched.total},
        warnings=table.warnings, files={"private_apps": file},
    )


def run_users_groups_export(
    tenant: str, token: str, include_deleted: bool, now: datetime | None = None, page_size: int | None = None
) -> ExportResult:
    now = now or utcnow()
    tenant = tenant.strip().lower()
    size = page_size or settings.data_export_page_size
    pacer = client.Pacer()                                    # ONE pacer for every call on the 4/s endpoints

    users = client.fetch_users(tenant, token, size, pacer=pacer)
    groups = client.fetch_groups(tenant, token, size, pacer=pacer)
    tables = build_users_groups_tables(users.rows, groups.rows, include_deleted)
    warnings = list(tables.warnings)

    # Soft cross-check, made last: a warning, never a failure. /users/counts may be cached.
    counts_total = None
    try:
        counts_total = client.fetch_user_counts(tenant, token, pacer=pacer)["totalUsers"]
    except NetskopeApiError:
        warnings.append("Could not read the tenant's user counts, so the live-account total was not cross-checked.")
    else:
        live = tables.stats["live_accounts"]
        if counts_total != live:
            warnings.append(
                f"Live accounts read ({live}) differ from the tenant's /users/counts total ({counts_total}). "
                f"That counts endpoint can be cached, so this is a warning, not a failure."
            )

    contents = {
        "users": build_csv_bytes(USERS_HEADER, tables.users_rows),
        "groups": build_csv_bytes(GROUPS_HEADER, tables.groups_rows),
        "memberships": build_csv_bytes(MEMBERSHIPS_HEADER, tables.memberships_rows),
    }
    row_counts = {"users": len(tables.users_rows), "groups": len(tables.groups_rows), "memberships": len(tables.memberships_rows)}
    files = {
        kind: ExportFile(kind, export_filename(tenant, kind, now), contents[kind], CSV_CONTENT_TYPE, row_counts[kind])
        for kind in ("users", "groups", "memberships")
    }
    files["users_groups_zip"] = ExportFile(
        kind="users_groups_zip",
        filename=export_filename(tenant, "users_groups", now, extension="zip"),
        content=build_zip_bytes({files[k].filename: contents[k] for k in ("users", "groups", "memberships")}),
        content_type=ZIP_CONTENT_TYPE,
        rows=None,
    )

    summary = dict(tables.stats)
    summary.update(
        users_total=users.total,
        groups_total=groups.total,
        counts_total_users=counts_total,
        include_deleted=include_deleted,
    )
    return ExportResult(
        kind="users_groups", tenant=tenant, created_at=now, options={"include_deleted": include_deleted},
        summary=summary, warnings=warnings, files=files,
        traces={"users": users.pages, "groups": groups.pages},
    )
