"""
Business logic for the nsdebug.log upload sub-feature of Device Posture
Validation (Operation 4). Read-only diagnostic on top of nsdebug_parser.py -
deliberately no Netskope API call and no tenant/token entry, since every
fact this feature reports lives inside the uploaded log itself (unlike the
rest of this operation, which queries the live tenant - see routes.py).

Kept as a thin view-model layer so nsdebug_parser.py (a standalone,
CLI-runnable module - see its own `if __name__ == "__main__"` block) stays
free of anything Jinja/FastAPI-specific, matching this project's existing
netskope_client.py/service.py/routes.py split in the other operations.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from enum import Enum
from pathlib import Path

from .netskope_client import (
    NetskopeApiError,
    fetch_classification_rules,
    find_check_nodes,
    iter_leaf_nodes,
    render_condition_tree,
    required_check_types,
    rules_for_os,
    split_condition_node,
)
from .nsdebug_parser import (
    DiskEncCycle,
    ParsedNsDebugLog,
    Verdict,
    mask_matches,
    parse_nsdebug_log,
    timestamp_seconds,
)

__all__ = [
    "parse_uploaded_log",
    "build_report_view",
    "cross_reference_rule",
    "evaluate_min_os_version_nodes",
    "evaluate_rule_tree",
    "attribute_summary",
    "evaluate_rules_for_device",
    "leaf_key",
    "Tri",
    "DISK_ENC_DERIVED_LABEL",
    "DERIVED_CONTRADICTION_WARNING",
]

# device_classification_custom_status_id sentinel for "device didn't
# satisfy any configured rule" - per the field engineer's own description
# of this value, not independently confirmed via a real captured log
# sample by this project yet.
_UNMANAGED_STATUS_ID = "-2"


def parse_uploaded_log(path: Path, source_name: str) -> ParsedNsDebugLog:
    return parse_nsdebug_log(path, source_name=source_name)


# Default row order for the "Per-check results" table: the actionable/
# uncertain outcomes an operator actually needs to look at first, then
# PASSED, then the two "nothing to see here" outcomes last. Ties (there
# won't usually be any - check_type is unique per row) keep whatever
# order summary_table() produced, since Python's sort() is stable.
_STATUS_SORT_PRIORITY = {
    "failed": 0,
    "mixed": 1,
    "unavailable": 2,
    "passed": 3,
    "no_rule": 4,
    "applicable_not_observed": 5,
}

# Evidence-list cap (Task 2 companion fix): a check type re-observed 100+
# times in a real log (e.g. Windows' "No rules for AV check") shouldn't
# force a long scroll to find the checks that matter. Show the first 3 +
# last 1 occurrence by default; the rest stay in the DOM (findable, and
# no server round-trip needed to reveal them) behind a "show N more
# lines" toggle (app/static/js/nsdebug-filter.js).
_EVIDENCE_CAP_HEAD = 3
_EVIDENCE_CAP_TAIL = 1


def _status_key(overall) -> str:
    """Lowercase key matching this project's existing `.status-<key>`
    CSS convention (style.css) - reused here rather than inventing a
    parallel badge vocabulary. `overall` is either a Verdict member or the
    literal string "MIXED" (see nsdebug_parser._resolve_mixed_verdict)."""
    return (overall.value if isinstance(overall, Verdict) else overall).lower()


def build_report_view(parsed: ParsedNsDebugLog) -> dict:
    """Flattens ParsedNsDebugLog into the exact shape
    nsdebug_report.html renders, so the template never has to know about
    Verdict/.value or dataclass attribute access."""
    rows = []
    for row in parsed.summary_table():
        overall = row["overall"]
        evidence = [
            {
                "verdict": e.verdict.value,
                "status_key": e.verdict.value.lower(),
                "line_no": e.line_no,
                "raw_line": e.raw_line.strip(),
                "detail": e.detail,
                "capped_hidden": False,
            }
            for e in row["evidence"]
        ]
        hidden_count = len(evidence) - _EVIDENCE_CAP_HEAD - _EVIDENCE_CAP_TAIL
        if hidden_count > 0:
            for ev in evidence[_EVIDENCE_CAP_HEAD:-_EVIDENCE_CAP_TAIL]:
                ev["capped_hidden"] = True
        else:
            hidden_count = 0

        rows.append({
            "check_type": row["check_type"],
            "overall_label": overall.value if isinstance(overall, Verdict) else overall,
            "status_key": _status_key(overall),
            "evidence": evidence,
            "hidden_evidence_count": hidden_count,
        })

    disk_encryption = _build_disk_encryption_view(parsed)
    if disk_encryption is not None:
        # Only where the row would otherwise say "applicable, not observed": a
        # verdict line the device logged itself (or an explicit "no rules"
        # line) is never second-guessed by a derived value.
        for row in rows:
            latest = disk_encryption["latest_cycle"]
            if (
                row["check_type"] == "disk_enc_check"
                and row["status_key"] == "applicable_not_observed"
                and latest["derived"]["verdict"] in ("TRUE", "FALSE")
            ):
                row["derived"] = {
                    "verdict": latest["derived"]["verdict"],
                    "label": DISK_ENC_DERIVED_LABEL,
                    "cycle_timestamp": latest["timestamp"],
                }

    rows.sort(key=lambda r: _STATUS_SORT_PRIORITY.get(r["status_key"], 99))

    snapshot = None
    if parsed.latest_snapshot is not None:
        s = parsed.latest_snapshot
        snapshot = {
            "timestamp": s.timestamp,
            "event": s.event,
            "os": s.os,
            "os_version": s.os_version,
            "hostname": s.hostname,
            "device_make": s.device_make,
            "device_model": s.device_model,
            "classification_status": s.classification_status,
            "custom_status_label": s.custom_status_label,
            "custom_status_id": s.custom_status_id,
        }

    return {
        "source_name": parsed.source_name,
        "platform": parsed.platform.value,
        "total_lines": parsed.total_lines,
        "snapshot_count": parsed.snapshot_count,
        "cache_fallback_count": parsed.cache_fallback_count,
        "snapshot": snapshot,
        "rows": rows,
        "warnings": parsed.warnings,
        # Not tied to a specific snapshot - this is a separate, one-per-log
        # registry-sourced line (see nsdebug_parser.py), same log-wide
        # treatment as cache_fallback_count above.
        "windows_edition": parsed.windows_edition,
        "device_info_summary": _build_summary_view(parsed),
        "disk_encryption": disk_encryption,
    }


# OS keys of the Debug-level device info summary block whose format has been
# verified against a real log, mapped to the `os` value of the tenant rules
# they correspond to. Only "win" has a real sample - the keys for
# mac/android/ios are deliberately NOT guessed (see attribute_summary).
_SUMMARY_OS_KEY_TO_RULE_OS = {"win": "windows"}


def _build_summary_view(parsed: ParsedNsDebugLog) -> dict | None:
    s = parsed.device_info_summary
    if s is None:
        return None
    return {
        "timestamp": s.timestamp,
        "line_no": s.line_no,
        "declared_len": s.declared_len,
        "captured_len": s.captured_len,
        "truncated": s.truncated,
        "os_key": s.os_key,
        "os_format_verified": s.os_key in _SUMMARY_OS_KEY_TO_RULE_OS,
        "block_count": s.block_count,
        "blocks_identical": s.blocks_identical,
        "block_use_cache": s.block_use_cache,
        "use_cache": s.use_cache,
        "cached_block_count": sum(1 for v in s.block_use_cache if v is True),
        "last_observed_type": s.last_observed_type,
        "entries_by_type": {
            check_type: [{"values": e.values, "status": e.status} for e in entries]
            for check_type, entries in s.entries_by_type.items()
        },
    }


# --------------------------------------------------------------------------
# DiskEnc volume lines -> derived disk-encryption evidence
#
# Windows logs `DiskEnc Disk Encryption: Volume: C:, status 1, mediatype:
# Fixed hard disk media` for every volume each evaluation cycle, but never the
# verdict of the rule leaf {"disk_enc_check": {"product": "bitlocker"}} - in a
# Debug log that leaf sits in the part of the summary block Netskope
# truncates. The mapping used here, "the leaf is TRUE iff every FIXED volume
# has status 1", is a HYPOTHESIS: one natural experiment (a device that became
# Windows-Posturing-COD only after C: and D: both reached 1) plus two managed
# devices that are consistent with it. So a result that rests on it is always
# DERIVED, never OBSERVED, and is checked against the device's own
# classification (see evaluate_rules_for_device) so the field can test it.
#
# It is applied to the bitlocker leaf ONLY. A real log shows why: one Windows log has
# `Disk encryption check: name bitlocker, status 1` and then, 3 ms later,
# `name pgp, status 0` - a volume's status says nothing about another
# product's criterion.
# --------------------------------------------------------------------------

DISK_ENC_DERIVED_LABEL = (
    "derived from volume encryption status "
    "(Netskope does not log this leaf verdict directly; mapping is unverified)"
)
DERIVED_CONTRADICTION_WARNING = (
    "derived disk result contradicts the actual classification; "
    "the volume->leaf mapping may be wrong for this device"
)

_VOLUME_STATUS_PRODUCT = "bitlocker"

# State-changes rows shown before "N more changes".
_STATE_CHANGE_ROW_CAP = 50


def _derive_disk_enc(cycle: DiskEncCycle) -> dict:
    """The disk_enc_check leaf's value implied by one cycle: an explicit "No
    rules" line first, then the fixed volumes (removable/other media are
    listed for information but never counted)."""
    if cycle.no_rule:
        return {"verdict": "NO_RULE", "reason": 'the client logged "No rules for disk encryption check" in this cycle'}
    fixed = [v for v in cycle.latest_by_volume() if v.is_fixed]
    if not fixed:
        return {"verdict": "UNKNOWN", "reason": "this cycle has no fixed-disk volume line"}
    not_encrypted = [v.volume for v in fixed if v.status == 0]
    if not_encrypted:
        return {"verdict": "FALSE", "reason": f"fixed volume(s) {', '.join(not_encrypted)} at status 0"}
    if all(v.status == 1 for v in fixed):
        return {"verdict": "TRUE", "reason": "every fixed volume is at status 1"}
    return {"verdict": "UNKNOWN", "reason": "a fixed volume logged a status other than 0 or 1"}


def _cycle_view(cycle: DiskEncCycle, reference_seconds: float | None = None) -> dict:
    latest = cycle.latest_by_volume()
    cycle_seconds = timestamp_seconds(cycle.timestamp)
    return {
        "timestamp": cycle.timestamp,
        "line_no": cycle.line_no,
        "seconds_before": (
            round(reference_seconds - cycle_seconds, 3)
            if reference_seconds is not None and cycle_seconds is not None else None
        ),
        "volumes": [
            {
                "volume": v.volume,
                "status": v.status,
                "mediatype": v.mediatype,
                "fixed": v.is_fixed,
                "counts": v.is_fixed,
                "line_no": v.line_no,
                "raw_line": v.raw_line,
            }
            for v in latest
        ],
        # Two evaluations a few seconds apart can share one cycle; only the
        # latest line per volume is shown then.
        "merged": len(cycle.volumes) != len(latest),
        "no_rule": cycle.no_rule,
        "derived": _derive_disk_enc(cycle),
    }


def _nearest_cycle_before(cycles: list[DiskEncCycle], seconds: list[float | None], at: float) -> DiskEncCycle | None:
    for cycle, cycle_seconds in zip(reversed(cycles), reversed(seconds)):
        if cycle_seconds is not None and cycle_seconds <= at:
            return cycle
    return None


def _state_changes_view(parsed: ParsedNsDebugLog, cycles: list[DiskEncCycle], seconds: list[float | None]) -> dict:
    """Each classification change with the DiskEnc volumes of the nearest cycle
    BEFORE it - newest first, capped. Read-only context for testing the
    volume hypothesis in the field: it shows changes that follow a disk
    change AND changes (flapping) that don't."""
    changes = parsed.classification_changes
    rows = []
    for change in reversed(changes[-_STATE_CHANGE_ROW_CAP:]):
        at = timestamp_seconds(change.timestamp)
        cycle = _nearest_cycle_before(cycles, seconds, at) if at is not None else None
        rows.append({
            "timestamp": change.timestamp,
            "line_no": change.line_no,
            "from_label": change.from_label,
            "to_label": change.to_label,
            "from_status": change.from_status,
            "to_status": change.to_status,
            "cycle": _cycle_view(cycle, at) if cycle is not None else None,
        })
    return {"rows": rows, "total": len(changes), "more": max(0, len(changes) - _STATE_CHANGE_ROW_CAP)}


def _build_disk_encryption_view(parsed: ParsedNsDebugLog) -> dict | None:
    cycles = parsed.disk_enc_cycles
    if not cycles:
        return None
    seconds = [timestamp_seconds(c.timestamp) for c in cycles]
    summary = parsed.device_info_summary
    block_at = timestamp_seconds(summary.timestamp) if summary is not None else None
    block_cycle = _nearest_cycle_before(cycles, seconds, block_at) if block_at is not None else None
    return {
        "cycle_count": len(cycles),
        "product": _VOLUME_STATUS_PRODUCT,
        "label": DISK_ENC_DERIVED_LABEL,
        "latest_cycle": _cycle_view(cycles[-1]),
        # The cycle nearest BEFORE the (latest) summary block - the one the
        # rule evaluation below may use.
        "block_cycle": _cycle_view(block_cycle, block_at) if block_cycle is not None else None,
        # True if the device logged its own per-product verdict line
        # (`Disk encryption check: name X, status N`) - then the report must
        # not claim Netskope never logs one.
        "has_direct_verdict_lines": any(
            e.verdict in (Verdict.PASSED, Verdict.FAILED) for e in parsed.check_evidence.get("disk_enc_check", [])
        ),
        "state_changes": _state_changes_view(parsed, cycles, seconds),
    }


def _parse_major_minor(version_str) -> tuple[int, int] | None:
    """Extracts (major, minor) from a version string, tolerating extra
    patch-level components - "26.5.2" and "26.5" are the same version for
    comparison purposes here (see evaluate_min_os_version_nodes)."""
    if not version_str:
        return None
    parts = re.findall(r"\d+", str(version_str))
    if not parts:
        return None
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    return (major, minor)


def _evaluate_numeric_min_os_version(threshold, device_os_version) -> str:
    threshold_tuple = _parse_major_minor(threshold)
    device_tuple = _parse_major_minor(device_os_version)
    if threshold_tuple is None or device_tuple is None:
        return "UNAVAILABLE"
    return "PASS" if device_tuple >= threshold_tuple else "FAIL"


# Matches a rule edition value of the confirmed real shape "Windows 10 All" /
# "Windows 11 All" - the "All" suffix means "any edition of this Windows
# version", so the real comparison is a family-prefix match, not exact
# string equality (CLAUDE.md 2026-09-27: "Windows 10 Home Single Language"
# satisfies "Windows 10 All").
_EDITION_ALL_RE = re.compile(r"^(?P<family>.+?)\s+All$", re.IGNORECASE)


def _evaluate_windows_edition(required_edition, device_edition) -> str:
    if not required_edition or not device_edition:
        return "UNAVAILABLE"
    m = _EDITION_ALL_RE.match(str(required_edition).strip())
    if m:
        family = m.group("family").strip().lower()
        return "PASS" if str(device_edition).strip().lower().startswith(family) else "FAIL"
    return "PASS" if str(device_edition).strip().lower() == str(required_edition).strip().lower() else "FAIL"


def evaluate_min_os_version_nodes(nodes: list[dict], snapshot: dict | None, windows_edition: str | None) -> list[dict]:
    """
    Computes each min_os_version_check leaf node's own PASS/FAIL/UNAVAILABLE
    verdict from raw device facts, for the confirmed real gap where the log
    itself never emits a native verdict for this check type on macOS (see
    cross_reference_rule's own docstring). Two genuinely different real
    condition shapes exist, confirmed 2026-09-27 via a real captured
    `GET deviceclassification/rules` response - do not assume one implies
    the other:

      - macOS / iOS / Android: {"min_os_version": "X.Y"} - a plain numeric
        major.minor threshold, compared against host_info.os_version.
        Patch-level granularity mismatches are not a mismatch at all (rule
        "12.0" vs device "26.5.2" is a clean PASS at the major.minor level).
      - Windows: {"min_os_version": "0", "edition": "Windows 10 All"} - the
        version number is a confirmed no-op ("0"); the real gate is
        `edition`, matched by FAMILY PREFIX against the Windows-Edition
        value captured from a separate registry-sourced log line
        (nsdebug_parser.py's `windows_edition`), never exact string
        equality.

    Every result here is a value AS4PUR computed from raw OS info + the
    rule's own threshold - NOT a verdict the device itself reported -
    callers must label it as such wherever it's rendered. Scope is
    deliberately narrow: only this one check type's own per-node verdict,
    never an attempt to evaluate a rule's full $and/$or tree into one
    overall verdict (a separate, out-of-scope feature).
    """
    results = []
    for params in nodes:
        edition = params.get("edition")
        threshold = params.get("min_os_version")
        if edition:
            verdict = _evaluate_windows_edition(edition, windows_edition)
            results.append({
                "description": f"edition matches: {edition}",
                "verdict": verdict,
                "device_value": windows_edition or "(no Windows Edition line found in this log)",
            })
        else:
            device_os_version = (snapshot or {}).get("os_version")
            verdict = _evaluate_numeric_min_os_version(threshold, device_os_version)
            results.append({
                "description": f"OS version is at least {threshold}",
                "verdict": verdict,
                "device_value": device_os_version or "(no os_version in this log's snapshot)",
            })
    return results


# --------------------------------------------------------------------------
# Debug-level device info summary -> per-rule evaluation
#
# The summary block is the de-duplicated UNION of every tenant rule's leaf
# conditions for one OS, in ascending rule id (verified on a real Windows
# log: the three Windows rules carry 6 min_os_version leaves between them
# but the block has 2 entries - identical leaves appear once). It is NOT
# one rule's criteria, and its entries carry no rule attribution of their
# own, so attribution is positional: the k-th observed entry of a check
# type pairs with the k-th leaf of that type in the union. mask() can't
# identify a value (distinct values collide), only VERIFY a pairing, so
# every pairing is checked and any that fails is reported as UNMATCHED
# instead of guessed.
# --------------------------------------------------------------------------

class Tri(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


def leaf_key(check_type: str, params: dict) -> str:
    """Identity of one rule leaf - what the union de-duplicates on, and what
    `observed` (below) is keyed by."""
    return f"{check_type}:{json.dumps(params, sort_keys=True)}"


def _tri(status) -> Tri:
    return Tri.UNKNOWN if status is None else (Tri.TRUE if status else Tri.FALSE)


def evaluate_rule_tree(rule_conditions, observed: dict) -> Tri:
    """
    Three-valued evaluation of a rule's $and/$or condition tree. `observed`
    maps leaf_key() -> True/False; a leaf that is absent (or None) is
    UNKNOWN - never false. Truncation in the Debug-level block means "the
    log didn't show it", which is not the same as "it failed".

      $and: any FALSE -> FALSE, else any UNKNOWN -> UNKNOWN, else TRUE
      $or:  any TRUE  -> TRUE,  else any UNKNOWN -> UNKNOWN, else FALSE

    An empty or malformed group is UNKNOWN rather than vacuously true/false.
    """
    kind, payload = split_condition_node(rule_conditions)
    if kind == "leaf":
        return _tri(observed.get(leaf_key(*payload)))
    if not payload:
        return Tri.UNKNOWN

    results = [evaluate_rule_tree(child, observed) for child in payload]
    if kind == "and":
        if Tri.FALSE in results:
            return Tri.FALSE
        return Tri.UNKNOWN if Tri.UNKNOWN in results else Tri.TRUE
    if Tri.TRUE in results:
        return Tri.TRUE
    return Tri.UNKNOWN if Tri.UNKNOWN in results else Tri.FALSE


def _failure_phrase(check_type: str, params: dict) -> str:
    if check_type == "process_check":
        name = params.get("process") or params.get("name") or params.get("process_name") or "(unspecified process)"
        return f"{name} not running"
    if check_type == "min_os_version_check":
        if params.get("edition"):
            return f"Windows edition is not {params['edition']}"
        return f"OS version is below {params.get('min_os_version')}"
    if check_type == "domain_check":
        return f"device is not on domain {', '.join(params.get('domains') or []) or '(unspecified)'}"
    if check_type == "disk_enc_check":
        product = params.get("product")
        return f"disk encryption ({product}) is not enabled" if product else "disk encryption is not enabled"
    return f"not satisfied: {render_condition_tree({check_type: params})}"


def _failing_leaves(node, observed: dict) -> list[str]:
    """For a node already known to be FALSE: the leaves that actually make it
    so - a FALSE leaf inside an $or that another alternative satisfies is not
    a reason the rule fails, so it is not listed."""
    kind, payload = split_condition_node(node)
    if kind == "leaf":
        return [_failure_phrase(*payload)]
    phrases: list[str] = []
    for child in payload or []:
        if evaluate_rule_tree(child, observed) == Tri.FALSE:
            phrases.extend(p for p in _failing_leaves(child, observed) if p not in phrases)
    return phrases


def _forced_true_leaves(node, observed: dict) -> set[str]:
    """Leaves whose truth is FORCED if `node` is known to be true: every
    child of an $and; the sole not-already-false alternative of an $or with
    no satisfied one. A leaf the log already showed is never re-labelled."""
    kind, payload = split_condition_node(node)
    if kind == "leaf":
        key = leaf_key(*payload)
        return {key} if observed.get(key) is None else set()
    forced: set[str] = set()
    if kind == "and":
        for child in payload:
            forced |= _forced_true_leaves(child, observed)
    elif kind == "or":
        results = [(child, evaluate_rule_tree(child, observed)) for child in payload]
        if not any(r == Tri.TRUE for _, r in results):
            candidates = [child for child, r in results if r != Tri.FALSE]
            if len(candidates) == 1:
                forced |= _forced_true_leaves(candidates[0], observed)
    return forced


def _rule_sort_key(rule: dict):
    rule_id = rule.get("id")
    try:
        return (0, int(rule_id), "")
    except (TypeError, ValueError):
        return (1, 0, str(rule_id))


def _expected_leaves(os_rules: list[dict]) -> dict[str, list[tuple[str, dict]]]:
    """The union the block encodes: per check type, every distinct leaf of
    every rule for this OS, ascending rule id, document order within a rule,
    identical leaves once."""
    expected: dict[str, list[tuple[str, dict]]] = {}
    seen: set[str] = set()
    for rule in sorted(os_rules, key=_rule_sort_key):
        for check_type, params in iter_leaf_nodes(rule.get("conditions") or {}):
            key = leaf_key(check_type, params)
            if key not in seen:
                seen.add(key)
                expected.setdefault(check_type, []).append((key, params))
    return expected


def _values_match(params: dict, entry: dict):
    """Does an observed entry's masked values fit a rule leaf's real ones?
    True (verified), False (contradicted), or None (nothing comparable was
    long enough to verify - "opaque"). Only keys present on both sides are
    compared, so a differently-named field can't cause a false mismatch."""
    verdicts = []
    for key, rule_value in params.items():
        if key not in entry["values"]:
            continue
        observed_value = entry["values"][key]
        if isinstance(rule_value, list) != isinstance(observed_value, list):
            verdicts.append(False)
        elif isinstance(rule_value, list):
            pairs = [mask_matches(str(r), str(o)) for r, o in zip(rule_value, observed_value)]
            if len(rule_value) != len(observed_value) or False in pairs:
                verdicts.append(False)
            else:
                verdicts.append(None if None in pairs else True)
        else:
            verdicts.append(mask_matches(str(rule_value), str(observed_value)))
    if False in verdicts:
        return False
    return True if True in verdicts else None


def _plural_entries(n: int) -> str:
    return f"{n} entry" if n == 1 else f"{n} entries"


def attribute_summary(summary: dict, os_rules: list[dict]) -> dict:
    """
    Pairs each observed summary entry with a rule leaf (see the block
    comment above). `os_rules` are the tenant rules for the summary's OS in
    any order. Returns:

      observed          leaf_key -> bool, for every pairing that was made
      leaves            leaf_key -> {check_type, params, state, status, reason}
                        state: OBSERVED (mask-verified) | OBSERVED_OPAQUE
                        (paired, but only short values - can't be verified) |
                        UNOBSERVED_TRUNCATED (the log was cut before it) |
                        UNMATCHED (contradicted, or no entry to pair with)
      warnings          per-type entry-count mismatches and failed pairings
      rule_set_changed  True if anything above says the tenant's rules are
                        not the ones this log was collected under

    Truncation cuts the block's TAIL, so a check type missing entirely - or
    the last type in the text running short - is expected when the block is
    truncated. Anything else that doesn't line up is a rule-set mismatch.
    """
    entries_by_type = summary["entries_by_type"]
    truncated = summary["truncated"]
    last_type = summary.get("last_observed_type")
    expected = _expected_leaves(os_rules)

    observed: dict[str, bool] = {}
    leaves: dict[str, dict] = {}
    warnings: list[str] = []
    rule_set_changed = False

    for check_type in [*expected, *(t for t in entries_by_type if t not in expected)]:
        exp = expected.get(check_type, [])
        obs = entries_by_type.get(check_type, [])
        cut_off = truncated and (not obs or check_type == last_type)

        if len(obs) != len(exp) and not (len(obs) < len(exp) and cut_off):
            warnings.append(
                f"{check_type}: this log lists {_plural_entries(len(obs))}, but the tenant's current rules "
                f"expect {len(exp)}."
            )
            rule_set_changed = True

        for i, (key, params) in enumerate(exp):
            info = {"check_type": check_type, "params": params, "status": None, "reason": None}
            if i < len(obs):
                match = _values_match(params, obs[i])
                status = obs[i]["status"]
                if match is False:
                    info["state"] = "UNMATCHED"
                    info["reason"] = f"the log's entry {i + 1} for {check_type} does not fit this rule value"
                    warnings.append(
                        f"{check_type} entry {i + 1}: the log's masked value does not match "
                        f"{render_condition_tree({check_type: params})!r} from the current rules."
                    )
                    rule_set_changed = True
                elif status is None:
                    info["state"] = "UNMATCHED"
                    info["reason"] = "the log's status for this entry could not be read"
                else:
                    info["state"] = "OBSERVED" if match else "OBSERVED_OPAQUE"
                    info["status"] = status
                    observed[key] = status
            elif cut_off:
                info["state"] = "UNOBSERVED_TRUNCATED"
                info["reason"] = "beyond the point where Netskope truncated this log entry"
            else:
                info["state"] = "UNMATCHED"
                info["reason"] = "the log has no entry for this leaf"
            leaves[key] = info

    return {
        "observed": observed,
        "leaves": leaves,
        "warnings": warnings,
        "rule_set_changed": rule_set_changed,
    }


_INFERRED_NOTE = (
    "inferred, not observed: the device's own classification says this rule matched, "
    "so this criterion must be satisfied"
)

# Plain-language names for the truncation note ("criteria after domain_check
# (e.g. disk encryption, registry) are not visible"). Unlisted check types
# fall back to their own type name rather than a guess.
_CHECK_TYPE_LABELS = {
    "disk_enc_check": "disk encryption",
    "reg_check": "registry",
    "av_check": "antivirus",
    "file_check": "file",
    "cert_check": "certificate",
    "client_cert_check": "client certificate",
    "opswat_check": "OPSWAT",
    "process_check": "process",
    "domain_check": "domain",
    "min_os_version_check": "OS version",
}


def _is_unmanaged(snapshot: dict) -> bool:
    return (
        snapshot.get("classification_status") == "unmanaged"
        or str(snapshot.get("custom_status_id") or "") == _UNMANAGED_STATUS_ID
    )


def evaluate_rules_for_device(
    summary: dict | None,
    snapshot: dict | None,
    rules: list[dict],
    disk_encryption: dict | None = None,
) -> dict | None:
    """
    Evaluates EVERY tenant rule for the summary block's OS against what the
    block actually shows, three-valued (see evaluate_rule_tree). Each rule
    gets:

      computed    TRUE/FALSE/UNKNOWN from mask-verified observed leaves only
      effective   computed, except: if the client's OWN classification says
                  this rule matched (snapshot label == rule label) and the
                  log can't settle it (UNKNOWN), it is TRUE - provenance
                  INFERRED, and any unobserved leaf that match FORCES true is
                  itself labelled INFERRED. If the client says it matched but
                  the observed leaves say FALSE, that is a conflict, not a
                  verdict.
      provenance  OBSERVED | INFERRED | DERIVED | UNKNOWN

    DERIVED (only when `disk_encryption` - the report's own view of the log's
    DiskEnc lines - is given): a bitlocker disk_enc_check leaf the truncated
    block never reached takes the value of the DiskEnc cycle nearest before
    the block (see the block comment above _derive_disk_enc). It can settle a
    rule that observed leaves alone leave UNKNOWN - but never one they already
    decide, and never as OBSERVED. If the derived answer contradicts the
    device's own classification (derived FALSE for the rule the device says it
    matched; derived TRUE for a rule while the device is unmanaged) the
    headline stays where it was without the derived leaf and the contradiction
    is reported in `derived_contradictions` - the hypothesis is what gets
    doubted, not the client.

    Matching to the client's classification is by LABEL, never id (a real
    log reported id 17906 for the rule whose live id is 17171), and an
    ambiguous label (shared by more than one live rule) is never used to
    infer anything.

    Only OS keys with a verified block format are attributed; any other is
    shown raw upstream and returns attributed=False here.
    """
    if not summary:
        return None

    evaluation = {
        "os_key": summary["os_key"],
        "attributed": False,
        "unattributed_reason": None,
        "rule_set_changed": False,
        "warnings": [],
        "derived_contradictions": [],
        "unobserved_criteria": [],
        "rules": [],
    }
    rule_os = _SUMMARY_OS_KEY_TO_RULE_OS.get(summary["os_key"])
    if rule_os is None:
        evaluation["unattributed_reason"] = (
            f'The device info summary is under OS key "{summary["os_key"]}", whose format is not yet verified '
            "against a real log - entries are shown raw, with no rule attribution."
        )
        return evaluation

    os_rules = rules_for_os(rules, rule_os)
    attribution = attribute_summary(summary, os_rules)
    observed = attribution["observed"]
    label_counts = Counter(r.get("label") for r in rules)
    snapshot = snapshot or {}
    client_label = snapshot.get("custom_status_label")
    client_id = str(snapshot.get("custom_status_id") or "")
    warnings = list(attribution["warnings"])
    derived_contradictions: list[str] = []

    # Leaves the block never reached that the DiskEnc cycle before it can
    # speak for: bitlocker disk_enc_check only, and only where the block was
    # truncated before it (a leaf the block DID show is OBSERVED, and wins).
    block_cycle = (disk_encryption or {}).get("block_cycle")
    disk_leaf_verdicts: dict[str, str] = {}
    if block_cycle is not None:
        for key, info in attribution["leaves"].items():
            if (
                info["check_type"] == "disk_enc_check"
                and info["state"] == "UNOBSERVED_TRUNCATED"
                and str(info["params"].get("product") or "").strip().lower() == _VOLUME_STATUS_PRODUCT
                and block_cycle["derived"]["verdict"] in ("TRUE", "FALSE", "NO_RULE")
            ):
                disk_leaf_verdicts[key] = block_cycle["derived"]["verdict"]
    derived_values = {k: v == "TRUE" for k, v in disk_leaf_verdicts.items() if v in ("TRUE", "FALSE")}

    results = []
    for rule in sorted(os_rules, key=_rule_sort_key):
        conditions = rule.get("conditions") or {}
        label = rule.get("label")
        computed = evaluate_rule_tree(conditions, observed)
        client_matched = bool(label) and label == client_label and client_id != _UNMANAGED_STATUS_ID and label_counts[label] == 1

        conflict = False
        inferred: set[str] = set()
        if client_matched and computed == Tri.FALSE:
            conflict = True
            effective, provenance = Tri.UNKNOWN, "UNKNOWN"
            warnings.append(
                f'The device reports it matched "{label}", but the criteria observed in this log say that rule '
                "cannot be satisfied - the rule set has probably changed since this log was collected."
            )
        elif client_matched and computed == Tri.UNKNOWN:
            effective, provenance = Tri.TRUE, "INFERRED"
            inferred = _forced_true_leaves(conditions, observed)
        elif computed == Tri.UNKNOWN:
            effective, provenance = Tri.UNKNOWN, "UNKNOWN"
        else:
            effective, provenance = computed, "OBSERVED"

        # A derived leaf is displayed as DERIVED, never relabelled INFERRED.
        rule_leaf_keys = {leaf_key(*node) for node in iter_leaf_nodes(conditions)}
        rule_derived = {k: v for k, v in derived_values.items() if k in rule_leaf_keys}
        inferred -= set(rule_derived)

        derived_contradiction = False
        used_derived = False
        if computed == Tri.UNKNOWN and rule_derived:
            with_derived = evaluate_rule_tree(conditions, {**observed, **derived_values})
            if with_derived != Tri.UNKNOWN:
                disagrees = (
                    (client_matched and with_derived == Tri.FALSE)
                    or (_is_unmanaged(snapshot) and with_derived == Tri.TRUE)
                )
                if disagrees:
                    derived_contradiction = True
                    reason = (
                        "the device reports it matched this rule"
                        if client_matched else "the device is unmanaged"
                    )
                    derived_contradictions.append(
                        f'{DERIVED_CONTRADICTION_WARNING} (rule "{label or rule.get("name")}": '
                        f"the volumes give {with_derived.value}, but {reason})."
                    )
                else:
                    effective, provenance, used_derived = with_derived, "DERIVED", True

        leaf_views = []
        seen: set[str] = set()
        for check_type, params in iter_leaf_nodes(conditions):
            key = leaf_key(check_type, params)
            if key in seen:
                continue
            seen.add(key)
            info = attribution["leaves"][key]
            status_value = _tri(observed.get(key)).value
            leaf_provenance = "OBSERVED" if status_value != Tri.UNKNOWN.value else "UNKNOWN"
            note = info["reason"]
            if info["state"] == "OBSERVED_OPAQUE":
                note = "value too short for Netskope's masking to be verified - paired by position only"
            if key in inferred:
                status_value, leaf_provenance, note = Tri.TRUE.value, "INFERRED", _INFERRED_NOTE
            leaf_disk = disk_leaf_verdicts.get(key)
            if leaf_disk in ("TRUE", "FALSE"):
                status_value, leaf_provenance = leaf_disk, "DERIVED"
                note = (
                    f"{DISK_ENC_DERIVED_LABEL} - cycle {block_cycle['timestamp']}: "
                    + ", ".join(f"{v['volume']} {v['status']}" for v in block_cycle["volumes"])
                )
            elif leaf_disk == "NO_RULE":
                status_value, leaf_provenance = "NO_RULE", "OBSERVED"
                note = (
                    'the client logged "No rules for disk encryption check" in the evaluation cycle '
                    "before this block, so it did not evaluate this criterion then"
                )
            leaf_views.append({
                "check_type": check_type,
                "description": render_condition_tree({check_type: params}),
                "status": status_value,
                "provenance": leaf_provenance,
                "note": note,
            })

        results.append({
            "rule_id": rule.get("id") or rule.get("_id") or rule.get("rule_id") or "(unknown id)",
            "name": rule.get("name") or rule.get("rule_name") or "(unnamed rule)",
            "label": label,
            "computed": computed.value,
            "effective": effective.value,
            "provenance": provenance,
            "client_matched": client_matched,
            "conflict": conflict,
            "derived_contradiction": derived_contradiction,
            "failing_leaves": (
                _failing_leaves(conditions, observed) if computed == Tri.FALSE
                else _failing_leaves(conditions, {**observed, **derived_values}) if used_derived and effective == Tri.FALSE
                else []
            ),
            "unknown_leaves": [lv["description"] for lv in leaf_views if lv["status"] == "UNKNOWN"],
            "leaves": leaf_views,
        })

    unobserved_types = list(dict.fromkeys(
        info["check_type"] for info in attribution["leaves"].values() if info["state"] == "UNOBSERVED_TRUNCATED"
    ))
    evaluation.update(
        attributed=True,
        rule_set_changed=attribution["rule_set_changed"],
        warnings=warnings,
        derived_contradictions=derived_contradictions,
        unobserved_criteria=[_CHECK_TYPE_LABELS.get(t, t) for t in unobserved_types],
        rules=results,
    )
    return evaluation


def cross_reference_rule(report: dict, tenant: str, token: str) -> dict:
    """
    Optional enrichment on top of the log-only report above (CLAUDE.md
    2026-09-27 nsdebug follow-up): looks up the live tenant rule matching
    this log's latest snapshot, so the report can show that rule's real
    name/conditions and flag any check type the LIVE RULE requires but
    which this log shows as APPLICABLE_NOT_OBSERVED (no local verdict at
    all).

    **Matches on the snapshot's `custom_status_label`
    (device_classification_custom_status) against a rule's own `label`
    field - NOT on `custom_status_id` against a rule's `id`.** Confirmed
    real bug, fixed 2026-09-27: a real captured Mac device reported
    custom_status_id "22200" and custom_status_label "MacOS-Posturing-COD",
    but the live rule actually named/labeled "MacOS-Posturing-COD" in that
    tenant had id 21393 - no rule with id 22200 existed at all (most likely
    a rule edit/recreation between when the log was captured and the live
    pull). The label matched exactly even though the id didn't. A rule's
    `name` and `label` can also differ from each other (confirmed real
    example: id 23166, name "Temp-MAC-Posturing", label
    "MacOS-Posturing-BYOD") - match on `label` specifically, never `name`.
    The original id-based version of this function shipped without this
    being caught because its own test fixture happened to use an id that
    trivially equaled the snapshot's status id - see
    tests/test_device_posture_nsdebug_upload.py for the permanent
    regression guard using a fixture where id and label deliberately
    differ, mirroring this real incident.

    The "-2" unmanaged sentinel is unaffected by this fix - it's still
    read from `custom_status_id`, unchanged.

    If more than one live rule shares the same label, this is a genuine
    tenant configuration ambiguity - it's surfaced as such rather than
    silently picking one.

    This closes a confirmed real gap, not a hypothetical one: a live rule
    named "MacOS-Posturing-COD" requires min_os_version_check AND
    process_check under Match ALL, but the matching device's real
    nsdebug.log never logs a native verdict for EITHER check type on
    macOS - so "no evidence in the log" does not reliably mean "no rule
    exists" for that check type, and this cross-reference is what turns
    that dead end into an actionable "go check this by hand" pointer (or,
    for min_os_version_check specifically, an AS4PUR-computed PASS/FAIL -
    see evaluate_min_os_version_nodes()).

    Never let a failure here affect the caller's already-built `report` -
    the log-only results must always still be trustworthy on their own;
    this only ever returns a side dict for the template to render
    alongside `report`, never mutates it.
    """
    snapshot = report.get("snapshot") or {}
    status_id = snapshot.get("custom_status_id")
    status_label = snapshot.get("custom_status_label")
    summary = report.get("device_info_summary")

    # A Debug-level device info summary block is worth evaluating against
    # the live rules even when the snapshot carries no rule attribution
    # (e.g. an unmanaged device - exactly when "which criteria fail" matters
    # most), so the block alone is reason enough to fetch them. A log
    # without one behaves exactly as before.
    if not status_id and not status_label and not summary:
        return _no_attribution_result()

    status_id = str(status_id) if status_id else None

    try:
        rules = fetch_classification_rules(tenant, token)
    except NetskopeApiError as exc:
        return {
            "attempted": True,
            "error": str(exc),
            "rule": None,
            "no_match_reason": None,
            "required_but_not_observed": [],
            "computed_os_version_check": [],
            "rule_evaluation": None,
        }

    result = _match_live_rule(report, snapshot, rules, status_id, status_label)
    result["rule_evaluation"] = evaluate_rules_for_device(summary, snapshot, rules, report.get("disk_encryption"))
    return result


def _no_attribution_result() -> dict:
    return {
        "attempted": True,
        "error": None,
        "rule": None,
        "no_match_reason": (
            "This log's latest classification snapshot has no rule-attribution label or ID "
            "(device_classification_custom_status/_id) to cross-reference against the live rule list."
        ),
        "required_but_not_observed": [],
        "computed_os_version_check": [],
        "rule_evaluation": None,
    }


def _match_live_rule(report: dict, snapshot: dict, rules: list[dict], status_id, status_label) -> dict:
    """The single-rule lookup half of cross_reference_rule() (see its
    docstring for the label-not-id matching rule and why) - split out only
    so the Debug-level all-rules evaluation can share the ONE live-rules
    fetch instead of making a second call."""
    if not status_id and not status_label:
        return _no_attribution_result()

    if status_id == _UNMANAGED_STATUS_ID:
        return {
            "attempted": True,
            "error": None,
            "rule": None,
            "no_match_reason": f'No matching rule found for id "{status_id}" - device did not satisfy any configured rule.',
            "required_but_not_observed": [],
            "computed_os_version_check": [],
        }

    if not status_label:
        return {
            "attempted": True,
            "error": None,
            "rule": None,
            "no_match_reason": (
                f'This log\'s latest snapshot has a rule-attribution ID ("{status_id}") but no rule label '
                "(device_classification_custom_status) - matching is done by label, not id, so this cannot "
                "be looked up against the live rule list."
            ),
            "required_but_not_observed": [],
            "computed_os_version_check": [],
        }

    matches = [r for r in rules if r.get("label") == status_label]

    if not matches:
        return {
            "attempted": True,
            "error": None,
            "rule": None,
            "no_match_reason": f'No matching rule found for label "{status_label}" in this tenant\'s live rule list.',
            "required_but_not_observed": [],
            "computed_os_version_check": [],
        }

    if len(matches) > 1:
        ids = ", ".join(str(r.get("id") or "(unknown id)") for r in matches)
        return {
            "attempted": True,
            "error": None,
            "rule": None,
            "no_match_reason": (
                f'{len(matches)} live rules share the label "{status_label}" (ids: {ids}) - cannot determine '
                "which one actually applies to this device. This is a tenant configuration ambiguity, not "
                "something safe to guess at."
            ),
            "required_but_not_observed": [],
            "computed_os_version_check": [],
        }

    matched = matches[0]
    conditions = matched.get("conditions") or {}
    not_observed = {row["check_type"] for row in report["rows"] if row["status_key"] == "applicable_not_observed"}
    gap_types = sorted(required_check_types(conditions) & not_observed)

    flagged = []
    computed_os_version_check: list[dict] = []
    for check_type in gap_types:
        if check_type == "min_os_version_check":
            nodes = find_check_nodes(conditions, "min_os_version_check")
            computed = evaluate_min_os_version_nodes(nodes, snapshot, report.get("windows_edition"))
            # Only substitute the computed result if every node actually
            # resolved to a real verdict - if the underlying data was
            # missing (UNAVAILABLE), fall back to the plain "check
            # manually" flag rather than presenting an inconclusive
            # computation as if it settled the question.
            if computed and all(n["verdict"] != "UNAVAILABLE" for n in computed):
                computed_os_version_check = computed
                continue
        flagged.append(check_type)

    return {
        "attempted": True,
        "error": None,
        "rule": {
            "rule_id": matched.get("id") or matched.get("_id") or matched.get("rule_id") or "(unknown id)",
            "name": matched.get("name") or matched.get("rule_name") or "(unnamed rule)",
            "label": matched.get("label"),
            "rendered_conditions": render_condition_tree(conditions),
        },
        "no_match_reason": None,
        "required_but_not_observed": flagged,
        "computed_os_version_check": computed_os_version_check,
    }
