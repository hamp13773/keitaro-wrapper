from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class StreamEdit(Base):
    __tablename__ = "stream_edits"

    tracker: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    base: Mapped[str | None] = mapped_column(String(64))
    base_offers: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    draft: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    published_signature: Mapped[str | None] = mapped_column(String(64))
    published: Mapped[list | None] = mapped_column(JSON(none_as_null=True))

    __mapper_args__ = {"version_id_col": version}


class StreamPublication(Base):
    __tablename__ = "stream_publications"
    __table_args__ = (
        Index("ix_stream_publications_stream", "tracker", "campaign_id", "stream_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tracker: Mapped[str] = mapped_column(String(64))
    campaign_id: Mapped[int]
    stream_id: Mapped[int]
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    before_offers: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    after_offers: Mapped[list] = mapped_column(JSON)


def make_sessions(url: str):
    options = {"timeout": 30} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=options)
    return engine, sessionmaker(engine, expire_on_commit=False)
