"""Store draft offers and published editor state per Keitaro flow."""

import sqlalchemy as sa
from alembic import op

revision = "0001_stream_edits"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stream_edits",
        sa.Column("tracker", sa.String(64), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), primary_key=True),
        sa.Column("stream_id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base", sa.String(64)),
        sa.Column("draft", sa.JSON()),
        sa.Column("published_signature", sa.String(64)),
        sa.Column("published", sa.JSON()),
    )


def downgrade() -> None:
    op.drop_table("stream_edits")
