"""
Single source of truth for "now" throughout this app.

SQLite has no native timezone-aware datetime type. SQLAlchemy's
`DateTime(timezone=True)` doesn't actually round-trip tzinfo through it -
a value written as timezone-aware UTC comes back from the database as a
*naive* datetime. Mixing aware and naive datetimes (e.g. comparing a
freshly-constructed `datetime.now(timezone.utc)` against a value just read
back from the database) eventually raises
`TypeError: can't compare offset-naive and offset-aware datetimes` the
first time a comparison happens to hit that combination.

The fix used throughout this app: every stored timestamp is naive, and is
always understood to mean UTC. Never construct a timestamp for a model or
a comparison against one any other way than `utcnow()` below.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
