"""Record verified applications of an offer distribution."""

import sqlalchemy as sa
from alembic import op

revision = "0003_stream_publications"
down_revision = "0002_draft_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stream_publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tracker", sa.String(64), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("stream_id", sa.Integer(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("before_offers", sa.JSON()),
        sa.Column("after_offers", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_stream_publications_stream",
        "stream_publications",
        ["tracker", "campaign_id", "stream_id", "id"],
    )


def downgrade() -> None:
    op.drop_table("stream_publications")
