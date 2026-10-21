"""Bind a verified Clerk user to one existing Eigent numeric account.

Revision ID: add_clerk_account_binding
Revises: provider_execution_revision
"""

import sqlalchemy as sa
from alembic import op

revision = "add_clerk_account_binding"
down_revision = "provider_execution_revision"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user", sa.Column("clerk_id", sa.String(255), nullable=True))
    op.create_index("ix_user_clerk_id", "user", ["clerk_id"], unique=True)


def downgrade():
    op.drop_index("ix_user_clerk_id", table_name="user")
    op.drop_column("user", "clerk_id")
