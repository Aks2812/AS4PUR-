"""
nsdebug_parser.py — Cross-platform nsdebug.log parser for AS4PUR's
"Device Posture Validation" sub-feature.

Design rationale (see chat for the full evidence trail this is built from):

  1. `clientStatusHandler.cpp:429`'s single-line `client_status` JSON block is
     the ONLY source confirmed present, with an identical schema, across
     Windows / macOS / Android samples. It carries the aggregate
     classification result, the rule attribution
     (`device_classification_custom_status_id` / `..._custom_status`), and
     host/OS info (`host_info.os`, `host_info.os_version`) in one place.
     It is therefore the PRIMARY source of truth here — not the per-check
     `deviceId.cpp` lines.

  2. `deviceId.cpp` per-check verdict lines are real, but their format is
     genuinely different per platform (different wording, different line
     numbers, different fields present) and on macOS/Android most check
     types never print anything at all (silence, not a "no rule" line —
     unlike Windows, which explicitly announces every unused check type
     every cycle). These are treated as SECONDARY/supplementary evidence,
     matched on stable MESSAGE TEXT rather than on `deviceId.cpp:<line>`
     line numbers, because line numbers are a compiled-binary artifact that
     can shift on any client version bump — coupling a parser to them is
     the kind of fragility that breaks silently on the next agent update.

  3. Two explicit gaps are modeled rather than papered over:
       - A check type Netskope's own docs say applies to this OS, but for
         which no confirmed log-line format exists yet in CHECK_PATTERNS,
         is reported as APPLICABLE_NOT_OBSERVED — never silently dropped
         and never guessed at.
       - A classification result served from stale cache (the client
         failed to fetch a fresh one and reused the last known value) is
         detected and surfaced, so a snapshot's "status" can be labeled
         `stale_cache` instead of being presented as a live, current verdict.

Reference: https://docs.netskope.com/en/device-classification-430812
(check-type-to-OS applicability; Netskope's public docs do not publish the
internal `deviceId.cpp` field/line names below — those come from direct
log evidence, not the docs).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import IO, Iterable, Iterator, Optional, Union


# --------------------------------------------------------------------------
# Platforms
# --------------------------------------------------------------------------

class Platform(str, Enum):
    WINDOWS = "windows"
    MACOS = "macos"
    IOS = "ios"
    ANDROID = "android"
    UNKNOWN = "unknown"


# host_info.os values observed in real client_status JSON blocks
_OS_FIELD_TO_PLATFORM = {
    "windows": Platform.WINDOWS,
    "mac": Platform.MACOS,
    "macos": Platform.MACOS,
    "ios": Platform.IOS,
    "android": Platform.ANDROID,
}

# Fallback platform sniffing from the process-name column when no
# client_status JSON block is present in the log at all (e.g. the iOS
# sample in hand never emitted one in its captured window).
_PROCESS_NAME_TO_PLATFORM = [
    (re.compile(r"\bstAgentiOS\b"), Platform.IOS),
    (re.compile(r"\bstAgentSvc\b"), Platform.WINDOWS),
    (re.compile(r"\bstAgentNE\b"), Platform.MACOS),
    (re.compile(r"\bnsclientlib\b"), Platform.ANDROID),
]


# --------------------------------------------------------------------------
# Check verdicts
# --------------------------------------------------------------------------

class Verdict(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NO_RULE = "NO_RULE"          # engine ran the check type; tenant has no rule for it
    UNAVAILABLE = "UNAVAILABLE"  # engine tried and could not read a verdict (e.g. MDM SDK missing)
    APPLICABLE_NOT_OBSERVED = "APPLICABLE_NOT_OBSERVED"  # doc says this OS supports it; not seen in this log


def _status_to_verdict(status_str: str) -> Verdict:
    return Verdict.PASSED if status_str == "1" else Verdict.FAILED


# --------------------------------------------------------------------------
# Per-OS check catalog — confirmed applicability from Netskope's own docs
# (https://docs.netskope.com/en/device-classification-430812), independent
# of whether this project has observed a matching log line yet.
# --------------------------------------------------------------------------

EXPECTED_CHECKS_BY_PLATFORM: dict[Platform, set[str]] = {
    Platform.WINDOWS: {
        "cert_check", "client_cert_check", "domain_check", "disk_enc_check",
        "opswat_check", "reg_check", "min_os_version_check", "process_check",
        "av_check", "file_check",
    },
    Platform.MACOS: {
        "cert_check", "domain_check", "disk_enc_check", "opswat_check",
        "min_os_version_check", "process_check", "av_check", "file_check",
    },
    Platform.IOS: {
        "min_os_version_check", "passcode_required_check",
        "device_not_compromised_check",
    },
    Platform.ANDROID: {
        "min_os_version_check", "passcode_required_check",
        "device_not_compromised_check", "primary_storage_encrypted_check",
        "mdm_check",
    },
}


@dataclass
class CheckPattern:
    check_type: str
    regex: re.Pattern
    # name of the capture group holding the pass/fail status digit, or None
    # if this pattern only ever means one fixed verdict (e.g. a "no rule"
    # line has no status digit at all).
    status_group: Optional[str] = None
    fixed_verdict: Optional[Verdict] = None


# Matched against the MESSAGE TEXT only — i.e. whatever follows
# "deviceId.cpp:<N> deviceId " (or the equivalent tag) on the line.
# Deliberately NOT keyed by line number — see module docstring.
CHECK_PATTERNS: dict[Platform, list[CheckPattern]] = {
    # Windows and macOS share the same underlying engine and message
    # wording wherever both have been observed (confirmed identical for
    # cert_check on real Windows + macOS logs) — one shared list.
    Platform.WINDOWS: [
        CheckPattern("cert_check", re.compile(r"^no cert check rule found$"), fixed_verdict=Verdict.NO_RULE),
        CheckPattern("domain_check", re.compile(r"^Domain check: status (?P<status>\d+), name (?P<name>.*?), len \d+$"), status_group="status"),
        CheckPattern("domain_check", re.compile(r"^No rules for domain check$"), fixed_verdict=Verdict.NO_RULE),
        CheckPattern("disk_enc_check", re.compile(r"^Disk encryption check: name (?P<name>\S+), status (?P<status>\d+)$"), status_group="status"),
        CheckPattern("disk_enc_check", re.compile(r"^No rules for disk encryption check$"), fixed_verdict=Verdict.NO_RULE),
        CheckPattern("opswat_check", re.compile(r"^No rule for OPSWAT check$"), fixed_verdict=Verdict.NO_RULE),
        CheckPattern("client_cert_check", re.compile(r"^No rules for client cert check$"), fixed_verdict=Verdict.NO_RULE),
        CheckPattern("reg_check", re.compile(r"^No rules for registry check$"), fixed_verdict=Verdict.NO_RULE),
        CheckPattern("min_os_version_check", re.compile(r"^OSVersion check: status (?P<status>\d+)$"), status_group="status"),
        CheckPattern("process_check", re.compile(r"^process check: status (?P<status>\d+), name (?P<name>.+)$"), status_group="status"),
        CheckPattern("av_check", re.compile(r"^No rules for AV check$"), fixed_verdict=Verdict.NO_RULE),
        CheckPattern("file_check", re.compile(r"^No rules for file check$"), fixed_verdict=Verdict.NO_RULE),
    ],
    Platform.ANDROID: [
        CheckPattern("min_os_version_check", re.compile(r"^DC OS version check status: (?P<status>\d+)$"), status_group="status"),
    ],
    Platform.IOS: [
        CheckPattern("min_os_version_check", re.compile(r"^Min Version Check: version = (?P<version>[\d.]+), status = (?P<status>\d+)$"), status_group="status"),
        CheckPattern("passcode_required_check", re.compile(r"^Passcode Check: status = (?P<status>\d+)$"), status_group="status"),
        CheckPattern("device_not_compromised_check", re.compile(r"^Not Compromised Check: status = (?P<status>\d+)$"), status_group="status"),
    ],
}
CHECK_PATTERNS[Platform.MACOS] = CHECK_PATTERNS[Platform.WINDOWS]

# mdm_check on Android is never a clean status line — it's one of these two
# "couldn't even read the config" failures. Modeled separately because
# pretending this is a PASS/FAIL verdict would misrepresent what the
# device actually reported.
ANDROID_MDM_UNAVAILABLE_PATTERNS = [
    re.compile(r"Failed to get ns_mdm_check from appRestrictions"),
    re.compile(r"Failed to get airwatch sdk Instance error: AirWatchSDKException.*"),
]

# The line that actually carries platform-tagged check messages, regardless
# of which platform's binary/source file emitted it.
_DEVICEID_LINE_RE = re.compile(r"deviceId\.cpp:\d+\s+deviceId\s+(?P<msg>.*)$")

_CLIENT_STATUS_RE = re.compile(
    r"clientStatusHandler\.cpp:429\s+clientStatusHandler\s+client status message:\s*(?P<json>\{.*\})\s*$"
)

_CACHE_FALLBACK_ERROR_RE = re.compile(r"Failed to download device classification status, Error: -2")
_CACHE_FALLBACK_WARN_RE = re.compile(r"downloadDeviceClassificationStatus failed\. Reusing the status from cache")

# Windows-only registry-sourced edition string (e.g. "Windows 10 Home Single
# Language") - the real gate for a Windows min_os_version_check condition,
# whose own "min_os_version" value is a confirmed no-op ("0") - see
# service.py's OS-version comparator, added 2026-09-27 alongside this line.
_WINDOWS_EDITION_RE = re.compile(
    r"version\.cpp:491\s+nsUtils\s+From Registry - Windows Edition:\s*(?P<edition>.+?)\s+BuildVersion:\s*(?P<build>\d+)"
)

_TIMESTAMP_RE = re.compile(r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+)")

# Debug-level only. Matched on MESSAGE TEXT, never on `deviceId.cpp:<line>`
# (module docstring, point 2) - so these are anchored on "deviceId " and the
# wording alone. The block's JSON body is on the FOLLOWING lines, none of
# which carry a timestamp prefix.
_SUMMARY_HEADER_RE = re.compile(r"deviceId\s+device info summary \(len:\s*(?P<len>\d+)\):\s*\{\s*$")
_USE_CACHE_RE = re.compile(r"deviceId\s+sessionId:\s*\d+,\s*useCache:\s*(?P<value>true|false)\b")

# Tolerant JSON-ish tokenizer for the summary block: a quoted string, a
# structural character, or a bare literal (true/false/number).
_SUMMARY_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]:,]|[^\s{}\[\]:,"]+')

# Windows logs one line per disk volume every evaluation cycle. Matched on
# MESSAGE TEXT only (module docstring, point 2) - never `diskEnc.cpp:<line>`.
#   2026/09/29 09:41:14.307 stAgentSvc p83cc t3a9c info diskEnc.cpp:426 DiskEnc Disk Encryption: Volume: C:, status 1, mediatype: Fixed hard disk media
_DISK_ENC_RE = re.compile(
    r"DiskEnc Disk Encryption: Volume: (?P<vol>[A-Za-z]):, status (?P<st>\d+), mediatype: (?P<media>.+?)\s*$"
)

# Lines this close together (seconds) belong to one evaluation cycle. Real
# logs sit right at this boundary - two back-to-back evaluations 4.2 s apart
# merge, a pair 5.14 s apart do not - hence DiskEncCycle.latest_by_volume().
DISK_ENC_CYCLE_GAP_SECONDS = 5.0

_EPOCH = datetime(1970, 1, 1)


def timestamp_seconds(ts: Optional[str]) -> Optional[float]:
    """Seconds for a logged `YYYY/MM/DD HH:MM:SS.mmm` wall-clock time, for
    ordering and gaps only. Plain arithmetic on the naive value - never
    `.timestamp()`, which would silently reinterpret it in the host's local
    timezone (a real bug elsewhere in this operation, see the project notes,
    Section 9) - and never an instant to send anywhere."""
    if not ts:
        return None
    try:
        return (datetime.strptime(ts, "%Y/%m/%d %H:%M:%S.%f") - _EPOCH).total_seconds()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Result model
# --------------------------------------------------------------------------

@dataclass
class CheckEvidence:
    check_type: str
    verdict: Verdict
    line_no: int
    raw_line: str
    detail: dict = field(default_factory=dict)  # e.g. {"name": "stAgentUI.exe"} or {"version": "15.0"}


@dataclass
class ClientStatusSnapshot:
    line_no: int
    timestamp: Optional[str]
    event: Optional[str]
    os: Optional[str]
    os_version: Optional[str]
    hostname: Optional[str]
    device_make: Optional[str]
    device_model: Optional[str]
    classification_status: Optional[str]            # device_classification_status
    custom_status_label: Optional[str]               # device_classification_custom_status
    custom_status_id: Optional[str]                  # device_classification_custom_status_id
    is_stale_cache: bool = False                     # true if the closest-preceding fetch fell back to cache
    raw: dict = field(default_factory=dict)


@dataclass
class ClassificationChange:
    """A client_status snapshot whose rule label differs from the previous
    snapshot's (the first snapshot in a log is not a change - there is nothing
    to compare it to)."""
    timestamp: Optional[str]
    line_no: int
    from_label: Optional[str]
    to_label: Optional[str]
    from_status: Optional[str]      # device_classification_status ("managed"/"unmanaged")
    to_status: Optional[str]


@dataclass
class DiskEncVolume:
    volume: str                     # "C:"
    status: int                     # as logged; only 0 and 1 have ever been seen
    mediatype: str                  # "Fixed hard disk media" on every real line seen
    timestamp: Optional[str]
    line_no: int
    raw_line: str

    @property
    def is_fixed(self) -> bool:
        return "fixed hard disk" in self.mediatype.lower()


@dataclass
class DiskEncCycle:
    """The DiskEnc volume lines of one evaluation (<= DISK_ENC_CYCLE_GAP_SECONDS
    apart). `volumes` keeps every logged line verbatim, in order."""
    timestamp: Optional[str]        # of the first line
    line_no: int                    # of the first line
    volumes: list[DiskEncVolume]
    # An explicit "No rules for disk encryption check" line within the same
    # window - the client announcing it did NOT evaluate this criterion.
    no_rule: bool = False

    def latest_by_volume(self) -> list[DiskEncVolume]:
        """One line per volume letter - the latest, since two evaluations a
        few seconds apart can land in one cycle. Ordered by first appearance."""
        latest: dict[str, DiskEncVolume] = {}
        for v in self.volumes:
            latest[v.volume] = v
        return list(latest.values())


def parse_disk_enc_line(line: str, line_no: int = 0) -> Optional[DiskEncVolume]:
    m = _DISK_ENC_RE.search(line)
    if not m:
        return None
    ts = _TIMESTAMP_RE.match(line)
    return DiskEncVolume(
        volume=m.group("vol").upper() + ":",
        status=int(m.group("st")),
        mediatype=m.group("media"),
        timestamp=ts.group("ts") if ts else None,
        line_no=line_no,
        raw_line=line.rstrip("\r\n"),
    )


def _within_gap(a: Optional[float], b: Optional[float], gap: float) -> bool:
    # Rounded to the log's own millisecond resolution: epoch-sized floats
    # can't otherwise tell "exactly 5.000 s" from "5.0000002 s".
    return a is not None and b is not None and round(abs(a - b), 3) <= gap


def group_disk_enc_cycles(
    volumes: list[DiskEncVolume],
    no_rule_times: Iterable[float] = (),
    gap_seconds: float = DISK_ENC_CYCLE_GAP_SECONDS,
) -> list[DiskEncCycle]:
    """Groups volume lines (in log order) into cycles: a line within
    `gap_seconds` of the PREVIOUS one joins its cycle. `no_rule_times` are the
    timestamps (timestamp_seconds) of "No rules for disk encryption check"
    lines; a cycle is `no_rule` if one falls within `gap_seconds` of any of
    its lines."""
    cycles: list[DiskEncCycle] = []
    last_t: Optional[float] = None
    for v in volumes:
        t = timestamp_seconds(v.timestamp)
        if cycles and _within_gap(t, last_t, gap_seconds):
            cycles[-1].volumes.append(v)
        else:
            cycles.append(DiskEncCycle(timestamp=v.timestamp, line_no=v.line_no, volumes=[v]))
        last_t = t

    no_rule_list = list(no_rule_times)
    if no_rule_list:
        for cycle in cycles:
            times = [timestamp_seconds(v.timestamp) for v in cycle.volumes]
            cycle.no_rule = any(_within_gap(nr, t, gap_seconds) for nr in no_rule_list for t in times)
    return cycles


# --------------------------------------------------------------------------
# Debug-level "device info summary" block (deviceId ... device info summary)
#
# Facts, all verified against a real Windows Debug-level log (not assumed):
#   - Netskope's own logger TRUNCATES this block: the header declares
#     `len: 4728`, only ~4062 chars are actually written, and the text ends
#     mid-structure. So it is never json.loads()'d - a tolerant scanner
#     reads whatever complete entries are present, and everything after the
#     cut is simply absent (never "failed", never "no rule").
#   - String values are masked (see mask()).
#   - It is the de-duplicated UNION of every tenant rule's leaf conditions
#     for that OS, ascending rule id - NOT one rule's criteria. Each entry's
#     `status` says whether that single leaf criterion is satisfied on this
#     device. Attributing entries back to rule leaves is service.py's job.
# --------------------------------------------------------------------------

def mask(value: str) -> str:
    """Netskope's masking of a logged string value, verified to reproduce
    27/27 real process names (and the domain/edition values) in a real
    Debug-level Windows log: first 3 chars, the 3 chars around the middle,
    last 3 chars, dot-joined. Different plain values can collide
    (mfeatp.exe and mfetp.exe both -> "mfe...tp....exe"; "Windows 10 All"
    and "Windows 11 All" both -> "Win...s 1...All"), so a mask can VERIFY a
    guess but never identify a value on its own."""
    c = len(value) // 2
    return value[:3] + "..." + value[c - 1:c + 2] + "..." + value[-3:]


# The shortest of the 27 real process names the algorithm above was verified
# on is 8 chars. Shorter values are masked differently ("0" is logged as
# "0..0") and no real sample pins down how, so they can't be checked.
_MIN_VERIFIED_MASK_LEN = 8


def mask_matches(value: str, observed: str) -> Optional[bool]:
    """True/False if `observed` is/isn't mask(value); None ("opaque") for a
    short value whose masking this parser cannot reproduce."""
    if mask(value) == observed:
        return True
    if len(value) < _MIN_VERIFIED_MASK_LEN:
        return None
    return False


@dataclass
class SummaryEntry:
    values: dict                # masked param values exactly as logged (str, or list[str] for e.g. domains)
    status: Optional[bool]      # the leaf criterion's own status; None if the log gave something unrecognisable


@dataclass
class DeviceInfoSummary:
    timestamp: Optional[str]            # of the LAST block - the current state
    line_no: int                        # header line of the last block
    declared_len: int                   # `len: N` from the header
    captured_len: int                   # chars of the JSON text actually present in the log
    truncated: bool                     # captured_len < declared_len
    os_key: Optional[str]               # "win" on the only real sample; never assumed for other OSes
    entries_by_type: dict[str, list[SummaryEntry]]
    block_count: int
    blocks_identical: bool
    block_use_cache: list[Optional[bool]]   # per block, in order; None if no preceding session line
    last_observed_type: Optional[str]       # last check type present in the captured text

    @property
    def use_cache(self) -> Optional[bool]:
        return self.block_use_cache[-1] if self.block_use_cache else None


def _attach(frame: list, value) -> None:
    kind, container, pending = frame
    if kind == "arr":
        container.append(value)
    elif pending is not None:
        container[pending] = value
        frame[2] = None


def _scan_summary_text(text: str) -> tuple[dict, set[int]]:
    """Tolerant scan of the (possibly truncated) block text after its
    header's opening brace. Containers are attached to their parent on
    OPEN, so whatever was captured survives a cut at any point; the ids of
    containers still open at EOF are returned so the caller can discard
    those incomplete entries rather than trust half an entry."""
    root: dict = {}
    stack: list[list] = [["obj", root, None]]  # frame: [kind, container, pending_key]
    for m in _SUMMARY_TOKEN_RE.finditer(text):
        tok = m.group(0)
        frame = stack[-1]
        if tok == "{" or tok == "[":
            child = {} if tok == "{" else []
            _attach(frame, child)
            stack.append(["obj" if tok == "{" else "arr", child, None])
        elif tok == "}" or tok == "]":
            if len(stack) > 1:
                stack.pop()
        elif tok == ",":
            if frame[0] == "obj":
                frame[2] = None
        elif tok == ":":
            continue
        else:
            value = tok[1:-1].replace('\\"', '"').replace("\\\\", "\\") if tok.startswith('"') else tok
            if frame[0] == "obj" and frame[2] is None:
                frame[2] = value  # this string is a key
            else:
                _attach(frame, value)
    return root, {id(f[1]) for f in stack[1:]}


def _parse_summary_status(raw) -> Optional[bool]:
    return {"true": True, "false": False}.get(str(raw).strip().lower()) if raw is not None else None


def _summary_entries(root: dict, open_ids: set[int]) -> tuple[Optional[str], dict[str, list[SummaryEntry]], list[str], Optional[str]]:
    rules = root.get("device_classification_rules")
    if not isinstance(rules, dict):
        return None, {}, [], None
    os_keys = [k for k, v in rules.items() if isinstance(v, dict)]
    if not os_keys:
        return None, {}, [], None

    checks = rules[os_keys[0]]
    entries_by_type: dict[str, list[SummaryEntry]] = {}
    for check_type, raw in checks.items():
        items = raw if isinstance(raw, list) else [raw]
        entries = [
            SummaryEntry(
                values={k: v for k, v in item.items() if k != "status"},
                status=_parse_summary_status(item.get("status")),
            )
            for item in items
            if isinstance(item, dict) and id(item) not in open_ids
        ]
        if entries:
            entries_by_type[check_type] = entries
    last_type = next(reversed(checks), None)
    return os_keys[0], entries_by_type, os_keys[1:], last_type


def _finish_summary(blocks: list[dict]) -> tuple[Optional[DeviceInfoSummary], list[str]]:
    if not blocks:
        return None, []
    last = blocks[-1]
    lines = last["lines"]
    # "{" from the header, then a newline + each body line, minus the final
    # line's own terminator (that's the log record's, not part of the JSON).
    captured_len = 1 + (1 + len("\n".join(lines)) if lines else 0)

    root, open_ids = _scan_summary_text("\n".join(lines))
    os_key, entries_by_type, extra_os_keys, last_type = _summary_entries(root, open_ids)

    warnings: list[str] = []
    if extra_os_keys:
        warnings.append(
            f"Debug-level device info summary lists more than one OS key ({os_key}, {', '.join(extra_os_keys)}); "
            f"only \"{os_key}\" is shown."
        )
    return DeviceInfoSummary(
        timestamp=last["timestamp"],
        line_no=last["line_no"],
        declared_len=last["declared_len"],
        captured_len=captured_len,
        truncated=captured_len < last["declared_len"],
        os_key=os_key,
        entries_by_type=entries_by_type,
        block_count=len(blocks),
        blocks_identical=len({b["digest"] for b in blocks}) == 1,
        block_use_cache=[b["use_cache"] for b in blocks],
        last_observed_type=last_type,
    ), warnings


_REAL_EVALUATIONS = {Verdict.PASSED, Verdict.FAILED}


def _resolve_mixed_verdict(verdicts: set[Verdict]) -> Union[Verdict, str]:
    """
    Known rough edge, fixed: a check type that logs BOTH a "no rule" line
    and a rare real PASSED/FAILED line in the same capture (e.g. a rule
    was added/removed mid-session) used to collapse to an unhelpful
    "MIXED" label indistinguishable from a genuinely conflicting result
    (two different process_check lines, one pass one fail). A real
    evaluation is strictly more informative than "no rule was configured"
    for the SAME check type in the SAME log, so when the only non-real
    verdict mixed in is NO_RULE, bias toward the real evaluation instead
    of burying it under "MIXED" - the individual evidence lines (both the
    NO_RULE line and the real one) remain visible in `evidence` either
    way, so nothing is hidden by this choice.

    Genuinely conflicting real evaluations (PASSED and FAILED both
    present - e.g. two configured processes, one running one not) still
    report "MIXED": there is no single verdict that fairly represents
    that case, and picking one would hide the other. Any other mix
    involving UNAVAILABLE also stays "MIXED", since UNAVAILABLE is itself
    a meaningful, distinct signal ("engine couldn't read a verdict at
    all") that a real PASSED/FAILED shouldn't silently override.
    """
    real = verdicts & _REAL_EVALUATIONS
    if real and (verdicts - real) <= {Verdict.NO_RULE}:
        if len(real) == 1:
            return next(iter(real))
    return "MIXED"


@dataclass
class ParsedNsDebugLog:
    source_name: str
    platform: Platform
    total_lines: int
    latest_snapshot: Optional[ClientStatusSnapshot]
    snapshot_count: int
    check_evidence: dict[str, list[CheckEvidence]] = field(default_factory=dict)
    cache_fallback_count: int = 0
    warnings: list[str] = field(default_factory=list)
    windows_edition: Optional[str] = None
    device_info_summary: Optional[DeviceInfoSummary] = None  # Debug-level logs only
    disk_enc_cycles: list[DiskEncCycle] = field(default_factory=list)  # Windows logs that carry DiskEnc lines
    classification_changes: list[ClassificationChange] = field(default_factory=list)

    def summary_table(self) -> list[dict]:
        """
        One row per check type this OS is documented to support (per
        Netskope's device-classification docs), each labeled with the
        BEST verdict evidence found — never silently omitted.

        For a check type with multiple evidence lines (e.g. process_check,
        which logs one line per configured process name), all of them are
        returned under `evidence`, since a single verdict can't represent
        "stAgentUI.exe PASSED, abc.exe FAILED" as one row.
        """
        expected = EXPECTED_CHECKS_BY_PLATFORM.get(self.platform, set())
        # mdm_check is Android-only and isn't in CHECK_PATTERNS (see module
        # docstring) — still list it so its UNAVAILABLE evidence surfaces.
        all_types = sorted(expected | set(self.check_evidence.keys()))
        rows = []
        for check_type in all_types:
            evidence = self.check_evidence.get(check_type, [])
            if evidence:
                verdicts = {e.verdict for e in evidence}
                if len(verdicts) == 1:
                    overall = verdicts.pop()
                else:
                    overall = _resolve_mixed_verdict(verdicts)
                rows.append({
                    "check_type": check_type,
                    "overall": overall,
                    "evidence": evidence,
                })
            else:
                rows.append({
                    "check_type": check_type,
                    "overall": Verdict.APPLICABLE_NOT_OBSERVED,
                    "evidence": [],
                })
        return rows


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _detect_platform_from_json(payload: dict) -> Optional[Platform]:
    os_field = (
        payload.get("client_status", {})
        .get("host_info", {})
        .get("os", "")
    )
    return _OS_FIELD_TO_PLATFORM.get(os_field.strip().lower()) if os_field else None


def parse_nsdebug_log(
    source: Union[str, Path, IO[str], Iterable[str]],
    source_name: str = "nsdebug.log",
) -> ParsedNsDebugLog:
    """
    Stream-parses one nsdebug.log (Windows/macOS/iOS/Android) and returns a
    ParsedNsDebugLog. Streams line-by-line rather than loading the whole
    file into memory — these logs run 2-9MB+ per real sample in hand.

    `source` may be a path, an open text file handle, or any line iterable
    (so this can be fed a Django/FastAPI `UploadFile`'s decoded stream just
    as easily as a local path in a quick test).
    """
    lines_iter: Iterator[str]
    if isinstance(source, (str, Path)) and Path(source).exists():
        f = open(source, "r", errors="replace")
        lines_iter = iter(f)
        _owns_file = True
    else:
        lines_iter = iter(source)  # type: ignore[assignment]
        _owns_file = False

    platform = Platform.UNKNOWN
    total_lines = 0
    check_evidence: dict[str, list[CheckEvidence]] = {}
    snapshots: list[ClientStatusSnapshot] = []
    cache_fallback_count = 0
    warnings: list[str] = []
    windows_edition: Optional[str] = None
    pending_cache_fallback = False  # set when we just saw the error+warn pair
    summary_blocks: list[dict] = []           # Debug-level device info summary blocks, in file order
    open_block: Optional[dict] = None
    last_use_cache: Optional[bool] = None     # nearest preceding "sessionId: N, useCache: X" line
    disk_volumes: list[DiskEncVolume] = []    # DiskEnc volume lines, in file order
    disk_no_rule_times: list[float] = []      # "No rules for disk encryption check" lines

    try:
        for line_no, raw_line in enumerate(lines_iter, start=1):
            total_lines = line_no
            line = raw_line.rstrip("\n")

            # --- Debug-level device info summary block (multi-line) ---
            # A block runs from its header to the next TIMESTAMPED line, so a
            # stray lone \r (the real logs have some) can split a line but
            # can never end a block - only a timestamp can. The line that
            # does end it falls through to the normal handling below.
            if open_block is not None:
                if _TIMESTAMP_RE.match(line):
                    open_block["digest"] = hashlib.sha256("\n".join(open_block["lines"]).encode("utf-8", "replace")).digest()
                    summary_blocks.append(open_block)
                    open_block = None
                else:
                    open_block["lines"].append(line.rstrip("\r"))
                    continue
            if "useCache" in line:
                um = _USE_CACHE_RE.search(line)
                if um:
                    last_use_cache = um.group("value") == "true"
            if "device info summary" in line:
                hm = _SUMMARY_HEADER_RE.search(line)
                if hm:
                    ts_match = _TIMESTAMP_RE.match(line)
                    open_block = {
                        "line_no": line_no,
                        "timestamp": ts_match.group("ts") if ts_match else None,
                        "declared_len": int(hm.group("len")),
                        "use_cache": last_use_cache,
                        "lines": [],
                    }

            # --- platform sniff (only needed until confirmed once) ---
            if platform is Platform.UNKNOWN:
                for pattern, plat in _PROCESS_NAME_TO_PLATFORM:
                    if pattern.search(line):
                        platform = plat
                        break

            # --- DiskEnc volume lines. No `continue`: this line still has to
            # reach the cache-fallback adjacency reset below exactly as it did
            # before these lines were read at all. ---
            if "DiskEnc Disk Encryption" in line:
                dv = parse_disk_enc_line(line, line_no)
                if dv is not None:
                    disk_volumes.append(dv)

            # --- cache-fallback detection (adjacent error+warn pair) ---
            if _CACHE_FALLBACK_ERROR_RE.search(line):
                pending_cache_fallback = True
                continue
            if pending_cache_fallback and _CACHE_FALLBACK_WARN_RE.search(line):
                cache_fallback_count += 1
                pending_cache_fallback = False
                continue
            pending_cache_fallback = False

            # --- Windows Edition (registry-sourced, separate line from the
            # client_status block - see nsdebug_parser.py's module-level
            # regex comment) ---
            em = _WINDOWS_EDITION_RE.search(line)
            if em:
                windows_edition = em.group("edition").strip()
                continue

            # --- client_status JSON block (primary source) ---
            m = _CLIENT_STATUS_RE.search(line)
            if m:
                try:
                    payload = json.loads(m.group("json"))
                except json.JSONDecodeError:
                    warnings.append(f"line {line_no}: client_status JSON failed to parse, skipped")
                else:
                    cs = payload.get("client_status", {})
                    host = cs.get("host_info", {})
                    detected = _detect_platform_from_json(payload)
                    if detected:
                        platform = detected
                    ts_match = _TIMESTAMP_RE.match(line)
                    snapshots.append(ClientStatusSnapshot(
                        line_no=line_no,
                        timestamp=ts_match.group("ts") if ts_match else None,
                        event=cs.get("event"),
                        os=host.get("os"),
                        os_version=host.get("os_version"),
                        hostname=host.get("hostname"),
                        device_make=host.get("device_make"),
                        device_model=host.get("device_model"),
                        classification_status=cs.get("device_classification_status"),
                        custom_status_label=cs.get("device_classification_custom_status"),
                        custom_status_id=cs.get("device_classification_custom_status_id"),
                        raw=payload,
                    ))
                continue

            # --- Android MDM read failures (no deviceId.cpp line exists for this) ---
            if platform is Platform.ANDROID or platform is Platform.UNKNOWN:
                for pat in ANDROID_MDM_UNAVAILABLE_PATTERNS:
                    if pat.search(line):
                        check_evidence.setdefault("mdm_check", []).append(CheckEvidence(
                            check_type="mdm_check",
                            verdict=Verdict.UNAVAILABLE,
                            line_no=line_no,
                            raw_line=line,
                            detail={"reason": pat.pattern},
                        ))
                        break

            # --- per-check deviceId.cpp lines (secondary, platform-specific) ---
            dm = _DEVICEID_LINE_RE.search(line)
            if not dm:
                continue
            msg = dm.group("msg").strip()

            candidates = CHECK_PATTERNS.get(platform, [])
            if platform is Platform.UNKNOWN:
                # Haven't confirmed platform yet (no client_status block or
                # process-name marker seen so far) — try every known
                # pattern set rather than dropping real evidence.
                for plat_patterns in CHECK_PATTERNS.values():
                    candidates = candidates + plat_patterns

            for cp in candidates:
                cm = cp.regex.match(msg)
                if not cm:
                    continue
                if cp.fixed_verdict is not None:
                    verdict = cp.fixed_verdict
                    detail = {}
                else:
                    status_val = cm.group(cp.status_group)
                    verdict = _status_to_verdict(status_val)
                    detail = {k: v for k, v in cm.groupdict().items() if k != cp.status_group}
                check_evidence.setdefault(cp.check_type, []).append(CheckEvidence(
                    check_type=cp.check_type,
                    verdict=verdict,
                    line_no=line_no,
                    raw_line=line,
                    detail=detail,
                ))
                if cp.check_type == "disk_enc_check" and verdict is Verdict.NO_RULE:
                    ts_match = _TIMESTAMP_RE.match(line)
                    nr_time = timestamp_seconds(ts_match.group("ts")) if ts_match else None
                    if nr_time is not None:
                        disk_no_rule_times.append(nr_time)
                break  # first matching pattern wins
    finally:
        if _owns_file:
            f.close()

    if open_block is not None:  # block ran to end of file
        open_block["digest"] = hashlib.sha256("\n".join(open_block["lines"]).encode("utf-8", "replace")).digest()
        summary_blocks.append(open_block)
    device_info_summary, summary_warnings = _finish_summary(summary_blocks)
    warnings.extend(summary_warnings)

    # Mark snapshots that immediately followed a detected cache-fallback as
    # stale. A cache fallback error+warn pair means the NEXT emitted
    # snapshot (if any, before a fresh successful fetch) reflects a reused,
    # not freshly computed, classification result.
    if cache_fallback_count and snapshots:
        # Best-effort: without per-snapshot fetch correlation in this log
        # format, flag this explicitly as a log-wide caveat rather than
        # guessing which specific snapshot(s) were affected.
        warnings.append(
            f"{cache_fallback_count} cache-fallback event(s) detected "
            f"(client failed to fetch a fresh classification and reused a "
            f"cached one) — treat classification snapshots with caution "
            f"around those timestamps; this log format does not let a "
            f"parser attribute a specific snapshot as cached vs. live."
        )

    classification_changes = [
        ClassificationChange(
            timestamp=cur.timestamp,
            line_no=cur.line_no,
            from_label=prev.custom_status_label,
            to_label=cur.custom_status_label,
            from_status=prev.classification_status,
            to_status=cur.classification_status,
        )
        for prev, cur in zip(snapshots, snapshots[1:])
        if cur.custom_status_label != prev.custom_status_label
    ]

    latest = snapshots[-1] if snapshots else None
    if latest is None:
        warnings.append(
            "No clientStatusHandler.cpp:429 client_status block found in "
            "this log — classification status/OS-version/rule-id fields "
            "are unavailable; falling back to whatever deviceId.cpp check "
            "lines were found, if any."
        )
    if platform is Platform.UNKNOWN:
        warnings.append(
            "Could not determine platform (no client_status block and no "
            "recognized process-name marker) — check-type matching was "
            "attempted against all known platforms' patterns, which may "
            "produce false positives."
        )

    return ParsedNsDebugLog(
        source_name=source_name,
        platform=platform,
        total_lines=total_lines,
        latest_snapshot=latest,
        snapshot_count=len(snapshots),
        check_evidence=check_evidence,
        cache_fallback_count=cache_fallback_count,
        warnings=warnings,
        windows_edition=windows_edition,
        device_info_summary=device_info_summary,
        disk_enc_cycles=group_disk_enc_cycles(disk_volumes, disk_no_rule_times),
        classification_changes=classification_changes,
    )


# --------------------------------------------------------------------------
# Quick local test harness — not part of the AS4PUR wiring, just useful for
# `python nsdebug_parser.py path/to/nsdebug.log` sanity checks while
# integrating this into app/operations/device_posture/.
# --------------------------------------------------------------------------

def _render_cli(parsed: ParsedNsDebugLog) -> str:
    lines = [
        f"Source:            {parsed.source_name}",
        f"Detected platform: {parsed.platform.value}",
        f"Total log lines:   {parsed.total_lines}",
        f"client_status snapshots seen: {parsed.snapshot_count}",
        f"Cache-fallback events:        {parsed.cache_fallback_count}",
        "",
    ]
    if parsed.latest_snapshot:
        s = parsed.latest_snapshot
        lines += [
            "Latest classification snapshot:",
            f"  timestamp:        {s.timestamp}",
            f"  event:            {s.event}",
            f"  os / os_version:  {s.os} / {s.os_version}",
            f"  hostname:         {s.hostname}",
            f"  device:           {s.device_make} {s.device_model}",
            f"  classification:   {s.classification_status}",
            f"  rule label / id:  {s.custom_status_label} / {s.custom_status_id}",
            "",
        ]
    lines.append("Per-check summary (per docs.netskope.com/en/device-classification-430812):")
    for row in parsed.summary_table():
        overall = row["overall"]
        overall_str = overall.value if isinstance(overall, Verdict) else overall
        lines.append(f"  [{overall_str:<24}] {row['check_type']}")
        for ev in row["evidence"]:
            lines.append(f"        line {ev.line_no}: {ev.raw_line.strip()}")
    if parsed.warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in parsed.warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: python nsdebug_parser.py <path-to-nsdebug.log>")
        raise SystemExit(1)
    result = parse_nsdebug_log(sys.argv[1], source_name=sys.argv[1])
    print(_render_cli(result))
