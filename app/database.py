from sqlalchemy import JSON, Integer, String, create_engine
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
    draft: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    published_signature: Mapped[str | None] = mapped_column(String(64))
    published: Mapped[list | None] = mapped_column(JSON(none_as_null=True))

    __mapper_args__ = {"version_id_col": version}


def make_sessions(url: str):
    options = {"timeout": 30} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=options)
    return engine, sessionmaker(engine, expire_on_commit=False)
