"""add admin role

Revision ID: 9f347e768ef9
Revises: 8c5e482a5c47
Create Date: 2026-09-08 20:05:03.058175

No schema-level change here, confirmed via autogenerate (empty diff, see
PR discussion / chat log) - `users.role` is `sa.Enum(Role)` with no
`create_constraint=True` in app/models.py, so on SQLite it's a plain
VARCHAR(8) with no CHECK constraint at all; SQLAlchemy's Enum type
validates membership purely on the Python/ORM side. "admin" (5 chars) also
fits the existing VARCHAR(8) width without a length change. Adding
Role.ADMIN to the Python enum therefore needed no DDL - this migration
exists to carry the one real thing that DOES need to be tracked: promoting
void to admin, per Part B of the RBAC build, as an explicit, repeatable
data migration rather than a manual UPDATE run once and forgotten.

Confirmed empirically (not assumed) before writing the raw SQL below:
SQLAlchemy's default Enum binding persists the Python enum MEMBER NAME,
not its `.value` - the real users table already has 'OPERATOR'/'VIEWER'
stored (uppercase), not 'operator'/'viewer', and a throwaway round-trip
via the ORM with Role.ADMIN confirmed it stores 'ADMIN' the same way.
Using 'admin' here would silently fail to match anything the ORM itself
would ever query for.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9f347e768ef9'
down_revision: Union[str, Sequence[str], None] = '8c5e482a5c47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # No-op for schema, by design - see module docstring. The data change:
    # void is this app's first real admin account (per Part B's explicit
    # instruction). Matches zero rows harmlessly on any DB without a
    # 'void' user (a fresh install, a different deployment) rather than
    # erroring - this migration is safe to run everywhere in the chain.
    op.execute(sa.text("UPDATE users SET role = 'ADMIN' WHERE username = 'void'"))


def downgrade() -> None:
    """Downgrade schema."""
    # Reverts void specifically back to the role confirmed (by direct
    # query) to be true immediately before this migration - 'operator' -
    # not a generic/guessed default. Same zero-rows-is-fine safety as
    # upgrade() above.
    op.execute(sa.text("UPDATE users SET role = 'OPERATOR' WHERE username = 'void' AND role = 'ADMIN'"))
