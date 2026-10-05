"""User status: input handling and query building.

The API calls live in service.py and the rule matching in policy.py. What was
confirmed on a real tenant (details in the design doc):
- getusers: "eq" is case-sensitive, "sw"/"co" are not; paging is page/pageSize;
  projection is a list of separate field names.
- clientstatus: `username eq '<email>'`; hostnames can contain spaces and a curly apostrophe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_LOCAL_RE = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
# Real Mac hostnames look like "Name’s MacBook Air": letters, spaces, a curly apostrophe.
# ASCII ' is converted to ’ (so it can never break a quoted query); " \\ ; / etc. are rejected.
_HOST_RE = re.compile(r"^\w[\w .’-]{0,251}$")

KIND_IDENTITY = "email_or_upn"  # email and UPN look identical: name@domain
KIND_HOSTNAME = "hostname"

@dataclass(frozen=True)
class Query:
    kind: str | None
    value: str  # exactly as typed (case is preserved: user-management search is case-sensitive)
    error: str | None = None

    @property
    def key(self) -> str:
        """Lower-cased form for comparing across systems (never for querying)."""
        return self.value.lower()


def identity_filter(value: str, field: str = "accounts.userName") -> dict:
    """getusers filter for an email/UPN.

    CONFIRMED on a real tenant: "eq" is case-sensitive, but "sw" and "co" are
    case-insensitive (lower case input matched a name stored with a capital).
    So query with "sw" using the lower-cased value, then keep only exact matches
    on the portal side with pick_exact(). "sw" (not "co") so that user@example.com
    cannot pull in unrelated names such as xuser@example.com.
    """
    return {"and": [{field: {"sw": value.lower()}}, {"accounts.deleted": {"eq": False}}]}


# Confirmed on a real tenant: paging is {"page": 1, "pageSize": N} (1-based). {"offset": 1, "limit": N}
# is accepted but skips the first record (counts.offset is 0-based), so data comes back empty.
# Projection must be a list of SEPARATE field names ("id emails" in one string is rejected).
# "id" alone returns the user's email. The remaining names come from the spec's sample user and
# are still to be confirmed one by one (the API answers "Unsupported field was provided: X").
USER_PROJECTION = ["id", "givenName", "familyName", "emails", "accounts.userName", "accounts.active",
                   "accounts.provisioner", "accounts.parentGroups", "accounts.ou"]


# Fields tried in order until one finds the user. The UPN often differs from the email
# (user.one@example.org vs User.One@example.com), so a typed email may only match "emails".
IDENTITY_FIELDS = ("accounts.userName", "emails", "accounts.emails")


def getusers_body(value: str, page_size: int = 20, projection: list[str] | None = None,
                  field: str = IDENTITY_FIELDS[0]) -> dict:
    return {"query": {"paging": {"page": 1, "pageSize": page_size},
                      "filter": identity_filter(value, field),
                      "projection": list(projection or USER_PROJECTION)}}


def pick_exact(rows: list[dict], value: str) -> list[dict]:
    """Keep users whose userName or email equals value, ignoring case.

    sw can also return longer names (user@example.com matches user@example.com.test), so an exact,
    case-insensitive comparison is required before trusting a hit.
    """
    want = value.lower()
    out = []
    for r in rows or []:
        names = [str(a.get("userName", "")).lower() for a in (r.get("accounts") or []) if isinstance(a, dict)]
        mails = [str(e).lower() for e in (r.get("emails") or [])] + [str(r.get("id", "")).lower()]
        mails += [str(e).lower() for a in (r.get("accounts") or []) if isinstance(a, dict) for e in (a.get("emails") or [])]
        if want in names or want in mails:
            out.append(r)
    return out


SUGGEST_MIN = 3     # characters before suggestions are fetched
_SUGGEST_RE = re.compile(r"^[\w.@+'’ -]{%d,100}$" % SUGGEST_MIN)


def suggest_text(raw: str) -> str | None:
    """Partial input for type-ahead, or None when it is too short or has odd characters.

    Looser than classify_query (half an email or hostname is fine) but limited to
    the characters an email, UPN or hostname can contain.
    """
    q = re.sub(r" {2,}", " ", (raw or "").strip())
    return q if _SUGGEST_RE.match(q) else None


def classify_query(raw: str, allowed_domains: tuple[str, ...] = ()) -> Query:
    """Decide whether the input is an email/UPN or a hostname, and validate it.

    Never raises: returns a Query with .error set for bad input.
    """
    q = (raw or "").strip()
    if not q:
        return Query(None, "", "Enter an email, UPN or hostname.")
    if len(q) > 320:
        return Query(None, "", "That input is too long.")
    if "\\" in q:
        return Query(None, "", "Use the email/UPN (name@domain) or the hostname, not DOMAIN\\user.")

    if "@" in q:
        if q.count("@") != 1:
            return Query(None, "", "That does not look like an email or UPN.")
        local, domain = q.split("@")
        if not _LOCAL_RE.match(local) or not _DOMAIN_RE.match(domain.lower()):
            return Query(None, "", "That does not look like an email or UPN.")
        if allowed_domains and domain.lower() not in allowed_domains:
            return Query(None, "", "Only these domains are allowed: " + ", ".join(allowed_domains) + ".")
        return Query(KIND_IDENTITY, f"{local}@{domain}")

    if re.search(r"[^\S ]", q):  # tabs, newlines and other whitespace/control characters
        return Query(None, "", "That does not look like a hostname (letters, digits, space, dot, dash, underscore).")
    q = q.replace("'", "\u2019")
    q = re.sub(r" {2,}", " ", q)
    if not _HOST_RE.match(q):
        return Query(None, "", "That does not look like a hostname (letters, digits, space, dot, dash, underscore).")
    return Query(KIND_HOSTNAME, q)
