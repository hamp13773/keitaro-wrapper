"""Store the offer snapshot that a draft was based on."""

import sqlalchemy as sa
from alembic import op

revision = "0002_draft_baseline"
down_revision = "0001_stream_edits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stream_edits", sa.Column("base_offers", sa.JSON()))


def downgrade() -> None:
    op.drop_column("stream_edits", "base_offers")
