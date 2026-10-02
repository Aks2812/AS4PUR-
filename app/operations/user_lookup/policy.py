"""NPA rule matching (pure functions, no network).

Built from real rule shapes seen in a tenant (see tests/test_policy.py):
- users / userGroups / organization_units / device conditions can be ABSENT,
  and absent means "any".
- "enabled" and "rule_id" are strings ("1", "59").
- match_criteria_action.action_name is not only allow/block: "periodic_reauth"
  (with reauth_interval) exists, so any name other than block is treated as
  "grants access, with conditions" and shown verbatim.
- Device condition: built-in `classification` (["managed"/"unmanaged"]) and custom
  `device_classification_id` (strings, e.g. "21057") are two storage fields for
  what the console shows as ONE "device classification" choice, so they are OR-ed.
  INFERRED, not confirmed: flagged in the UI until checked against a rule.
- custom id "21057" equals clientstatus user_info.device_classification_custom_status_id
  (int there, string here): compare as strings.
- Private app names really contain square brackets: /steering/apps/private returns
  {"app_id": 375, "app_name": "[BeyondTrust Client]"} and rules use the same string
  (appId is null there). Names are shown as returned and compared without the brackets.
- Group conditions (userGroups) are implemented but UNVERIFIED: no rule in the
  tenant uses them yet, so their exact shape has not been seen.
- Rules have no order field, so conflicts are flagged, never resolved.
- Assumed (unverified): users / userGroups / organization_units are OR-ed together
  when several are present; different condition kinds (who, device, os) are AND-ed.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _lower_set(v) -> set[str]:
    if isinstance(v, (str, int)):
        v = [v]
    if not isinstance(v, (list, tuple, set)):
        return set()
    return {_s(x).lower() for x in v if _s(x)}


def app_name(v) -> str:
    """'[Fleet ELK]' -> 'Fleet ELK'."""
    n = _s(v)
    if len(n) >= 2 and n[0] == "[" and n[-1] == "]":
        n = n[1:-1].strip()
    return n


def canon_os(v) -> str:
    o = _s(v).lower()
    if not o:
        return ""
    for key, name in (("win", "windows"), ("mac", "mac"), ("ios", "ios"), ("ipad", "ios"),
                      ("android", "android"), ("linux", "linux"), ("chrome", "chromeos")):
        if key in o:
            return name
    return o


# ---- data ------------------------------------------------------------------
@dataclass(frozen=True)
class Identity:
    emails: frozenset[str]          # lower-cased; NPA rules match on email
    groups: frozenset[str] = frozenset()
    ous: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Device:
    hostname: str = ""
    device_id: str = ""
    classification: str = ""        # "Managed" / "Unmanaged" ...
    custom_id: str = ""
    custom_name: str = ""
    os: str = ""
    os_version: str = ""
    model: str = ""
    last_event_ts: int = 0          # epoch seconds, 0 = unknown
    last_event: str = ""            # last_seen_device_event.event, e.g. "Dem Heartbeat", "Uninstalled"
    npa_status: str = ""

    @property
    def uninstalled(self) -> bool:
        """The last thing this client reported was being uninstalled: not a live device."""
        return self.last_event.lower() == "uninstalled"

    def age_seconds(self, now: int) -> int | None:
        return max(0, now - self.last_event_ts) if self.last_event_ts else None

    @staticmethod
    def from_row(row: dict) -> "Device":
        row = row if isinstance(row, dict) else {}
        ui = row.get("user_info") if isinstance(row.get("user_info"), dict) else {}
        hi = row.get("host_info") if isinstance(row.get("host_info"), dict) else {}
        ev = row.get("last_seen_device_event") if isinstance(row.get("last_seen_device_event"), dict) else {}
        ts = row.get("last_event_timestamp") or row.get("timestamp") or 0
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            ts = 0
        return Device(
            hostname=_s(hi.get("hostname") or row.get("hostname")),
            device_id=_s(row.get("device_id") or hi.get("nsdeviceuid")),
            classification=_s(ui.get("device_classification_status")),
            custom_id=_s(ui.get("device_classification_custom_status_id")),
            custom_name=_s(ui.get("device_classification_custom_status")),
            os=_s(hi.get("os")),
            os_version=_s(hi.get("os_version")),
            model=" ".join(x for x in (_s(hi.get("device_make")), _s(hi.get("device_model"))) if x),
            last_event_ts=ts,
            last_event=_s(ev.get("event")),
            npa_status=_s(ev.get("npa_status")),
        )


def latest_per_device(rows: list[dict]) -> list[Device]:
    """clientstatus rows look like events: keep the newest row per device."""
    best: dict[str, Device] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        d = Device.from_row(r)
        key = d.device_id or d.hostname.lower() or f"row{len(best)}"
        if key not in best or d.last_event_ts >= best[key].last_event_ts:
            best[key] = d
    return sorted(best.values(), key=lambda d: -d.last_event_ts)


def groups_from_rows(rows: list[dict]) -> frozenset[str]:
    """clientstatus `usergroup` is a list of group-name strings: lower-cased union."""
    out: set[str] = set()
    for r in rows or []:
        if isinstance(r, dict):
            out |= _lower_set(r.get("usergroup"))
    return frozenset(out)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    name: str
    enabled: bool
    action: str
    action_note: str
    users: frozenset[str]
    groups: frozenset[str]
    ous: frozenset[str]
    classification: frozenset[str]
    custom_ids: frozenset[str]
    os: frozenset[str]
    access_methods: tuple[str, ...]
    apps: tuple[str, ...]
    tags: tuple[str, ...]
    user_type: str = "user"

    @property
    def has_who(self) -> bool:
        return bool(self.users or self.groups or self.ous)

    @property
    def has_device_class(self) -> bool:
        return bool(self.classification or self.custom_ids)

    @property
    def grants(self) -> bool:
        return self.action != "block"


def normalize_rule(raw) -> Rule | None:
    if not isinstance(raw, dict):
        return None
    rd = raw.get("rule_data") if isinstance(raw.get("rule_data"), dict) else {}
    mca = rd.get("match_criteria_action") if isinstance(rd.get("match_criteria_action"), dict) else {}
    action = _s(mca.get("action_name")).lower() or "unknown"
    note = ""
    pr = rd.get("periodic_reauth")
    if action == "periodic_reauth" and isinstance(pr, dict):
        note = f"re-auth every {_s(pr.get('reauth_interval'))} {_s(pr.get('reauth_interval_unit'))}".strip()

    names: list[str] = []
    for n in rd.get("privateApps") or []:
        names.append(_s(n))
    for it in rd.get("privateAppsWithActivities") or []:
        if isinstance(it, dict):
            names.append(_s(it.get("appName")))
    seen: dict[str, str] = {}
    for n in names:                                              # dedupe by bracket-less, case-less key
        if n:
            seen.setdefault(app_name(n).lower(), n)
    apps = tuple(seen.values())
    tags = tuple(dict.fromkeys(app_name(t) for t in rd.get("privateAppTags") or [] if app_name(t)))
    am = rd.get("access_method")
    return Rule(
        rule_id=_s(raw.get("rule_id")),
        name=_s(raw.get("rule_name")),
        enabled=_s(raw.get("enabled")) in ("1", "true", "True"),
        action=action, action_note=note,
        users=frozenset(_lower_set(rd.get("users"))),
        groups=frozenset(_lower_set(rd.get("userGroups"))),
        ous=frozenset(_lower_set(rd.get("organization_units"))),
        classification=frozenset(_lower_set(rd.get("classification"))),
        custom_ids=frozenset({_s(x) for x in (rd.get("device_classification_id") or []) if _s(x)}),
        os=frozenset(canon_os(x) for x in (rd.get("os") or []) if canon_os(x)),
        access_methods=tuple(_s(x) for x in am) if isinstance(am, list) else (),
        apps=apps, tags=tags,
        user_type=_s(rd.get("userType")) or "user",
    )


# ---- matching ---------------------------------------------------------------
APPLIES, DEVICE_MISMATCH, DEVICE_UNKNOWN, NOT_FOR_USER = "applies", "device_mismatch", "device_unknown", "not_for_user"


@dataclass
class Match:
    rule: Rule
    status: str
    via: list[str] = field(default_factory=list)        # why the user matched
    failed: list[str] = field(default_factory=list)     # which device condition failed


def who_matches(rule: Rule, ident: Identity) -> list[str]:
    if not rule.has_who:
        return ["everyone"]
    via = []
    if rule.users & ident.emails:
        via.append("user")
    if rule.groups & ident.groups:
        via.append("group")
    if rule.ous & ident.ous:
        via.append("ou")
    return via


def evaluate(rule: Rule, ident: Identity, device: Device | None) -> Match:
    if rule.user_type != "user":
        return Match(rule, NOT_FOR_USER)
    via = who_matches(rule, ident)
    if not via:
        return Match(rule, NOT_FOR_USER)
    wants_device = rule.has_device_class or bool(rule.os)
    if not wants_device:
        return Match(rule, APPLIES, via)
    if device is None:
        return Match(rule, DEVICE_UNKNOWN, via)
    failed = []
    if rule.has_device_class:
        ok = (device.classification.lower() in rule.classification
              or (device.custom_id != "" and device.custom_id in rule.custom_ids))
        if not ok:
            failed.append("device classification")
    if rule.os and canon_os(device.os) not in rule.os:
        failed.append("operating system")
    return Match(rule, DEVICE_MISMATCH if failed else APPLIES, via, failed)


def app_conflicts(matches: list[Match]) -> dict[str, dict[str, list[str]]]:
    """Apps that appear in an applying block rule AND an applying grant rule.

    Rule order is unknown, so the API cannot say which wins: report, don't decide.
    """
    per: dict[str, dict[str, list[str]]] = {}
    for m in matches:
        if m.status != APPLIES or not m.rule.enabled:
            continue
        for a in m.rule.apps:
            slot = per.setdefault(a, {"block": [], "grant": []})
            slot["grant" if m.rule.grants else "block"].append(m.rule.name or m.rule.rule_id)
    return {a: v for a, v in per.items() if v["block"] and v["grant"]}


def parse_private_apps(payload) -> dict[str, dict]:
    """GET /steering/apps/private -> {bracket-less lower name: {"id", "name"}}."""
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("private_apps") if isinstance(data, dict) else None
    out: dict[str, dict] = {}
    for it in items or []:
        if isinstance(it, dict) and _s(it.get("app_name")):
            out[app_name(it["app_name"]).lower()] = {"id": it.get("app_id"), "name": _s(it["app_name"])}
    return out


def match_policies(raw_rules: list[dict], ident: Identity, devices: list[Device]) -> dict:
    """Evaluate every enabled rule against each device (or once with no device)."""
    rules = [r for r in (normalize_rule(x) for x in raw_rules or []) if r and r.enabled]
    out = {}
    for d in (devices or [None]):
        key = (d.device_id or d.hostname) if d else ""
        ms = [evaluate(r, ident, d) for r in rules]
        out[key] = {
            "device": d,
            "applies": [m for m in ms if m.status == APPLIES],
            "blocked_by_device": [m for m in ms if m.status == DEVICE_MISMATCH],
            "device_unknown": [m for m in ms if m.status == DEVICE_UNKNOWN],
            "conflicts": app_conflicts(ms),
        }
    return out
