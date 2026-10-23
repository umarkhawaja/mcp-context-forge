# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/alembic/versions/d3f8a1c5e9b2_add_sso_team_external_source.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

add_sso_team_external_source

Revision ID: d3f8a1c5e9b2
Revises: c7e91a2b4d60
Create Date: 2026-10-23 09:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d3f8a1c5e9b2"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "c7e91a2b4d60"  # pragma: allowlist secret
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "email_teams"
INDEX = "uq_email_teams_external"
COLUMNS = (
    ("source", 20),
    ("external_provider", 255),
    ("external_id", 255),
)


def upgrade() -> None:
    """Add source/external_provider/external_id to email_teams and a partial unique index on the external pair."""
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return

    existing_columns = {col["name"] for col in inspector.get_columns(TABLE)}
    for name, length in COLUMNS:
        if name not in existing_columns:
            op.add_column(TABLE, sa.Column(name, sa.String(length), nullable=True))

    existing_indexes = {idx["name"] for idx in inspector.get_indexes(TABLE)}
    if INDEX not in existing_indexes:
        op.create_index(
            INDEX,
            TABLE,
            ["external_provider", "external_id"],
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL"),
            sqlite_where=sa.text("external_id IS NOT NULL"),
        )


def downgrade() -> None:
    """Drop the external-source index and columns from email_teams."""
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes(TABLE)}
    if INDEX in existing_indexes:
        op.drop_index(INDEX, table_name=TABLE)

    existing_columns = {col["name"] for col in inspector.get_columns(TABLE)}
    with op.batch_alter_table(TABLE) as batch_op:
        for name, _ in reversed(COLUMNS):
            if name in existing_columns:
                batch_op.drop_column(name)
