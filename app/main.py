from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi import Path as RoutePath
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.campaigns import (
    CampaignInput,
    VersionInput,
    campaign_summary,
    create_campaign,
    stream_summary,
)
from app.config import Settings
from app.database import StreamEdit, make_sessions
from app.demo import DemoKeitaro
from app.editor import DraftChange, Editor, editor_view, live_offers
from app.errors import AppError
from app.tracker import Keitaro

STATIC = Path(__file__).with_name("static")
ResourceId = Annotated[int, RoutePath(gt=0)]


def create_app(
    settings: Settings | None = None, transport: httpx.BaseTransport | None = None
) -> FastAPI:
    settings = settings or Settings()
    if transport is None and settings.keitaro_mode == "demo":
        transport = httpx.MockTransport(DemoKeitaro(settings.campaign_prefix).handle)
    tracker = Keitaro(settings, transport=transport)
    engine, session_factory = make_sessions(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            if not inspect(engine).has_table(StreamEdit.__tablename__):
                raise RuntimeError("Сначала примените миграции командой alembic upgrade head.")
            yield
        finally:
            tracker.client.close()
            engine.dispose()

    app = FastAPI(title="Keitaro Wrapper", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
        www_redirect=False,
    )

    def db_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    Db = Annotated[Session, Depends(db_session)]

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, error: AppError):
        return JSONResponse({"detail": error.message}, status_code=error.status)

    @app.exception_handler(IntegrityError)
    @app.exception_handler(StaleDataError)
    async def handle_conflict(_: Request, error: Exception):
        return JSONResponse(
            {"detail": "Черновик изменился. Обновите поток и повторите действие."}, status_code=409
        )

    @app.get("/api/config")
    def configuration():
        return {"prefix": settings.campaign_prefix, "mode": settings.keitaro_mode}

    @app.get("/api/catalogs")
    def catalogs():
        return tracker.catalogs()

    @app.get("/api/campaigns")
    def list_campaigns():
        return [campaign_summary(row) for row in tracker.campaigns()]

    @app.post("/api/campaigns", status_code=201)
    def add_campaign(body: CampaignInput):
        return create_campaign(tracker, body)

    @app.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: ResourceId):
        campaign = campaign_summary(tracker.campaign(campaign_id))
        if settings.keitaro_mode == "live":
            campaign["admin_url"] = f"{settings.keitaro_url}/admin/#!/campaigns/{campaign_id}"
        streams = [
            stream_summary(row)
            for row in tracker.streams(campaign_id)
            if row.get("state") != "deleted"
        ]
        return {"campaign": campaign, "streams": streams}

    @app.get("/api/campaigns/{campaign_id}/streams/{stream_id}")
    def get_editor(campaign_id: ResourceId, stream_id: ResourceId, db: Db):
        stream, edit = Editor(tracker, db).read(campaign_id, stream_id)
        return editor_view(edit, live_offers(stream))

    @app.get("/api/campaigns/{campaign_id}/streams/{stream_id}/stats")
    def get_stats(campaign_id: ResourceId, stream_id: ResourceId):
        tracker.campaign(campaign_id)
        tracker.stream(campaign_id, stream_id)
        return tracker.stats(campaign_id, stream_id)

    @app.post("/api/campaigns/{campaign_id}/streams/{stream_id}/draft")
    def change_draft(campaign_id: ResourceId, stream_id: ResourceId, body: DraftChange, db: Db):
        return Editor(tracker, db).change(campaign_id, stream_id, body)

    @app.post("/api/campaigns/{campaign_id}/streams/{stream_id}/draft/publish")
    def publish_draft(campaign_id: ResourceId, stream_id: ResourceId, body: VersionInput, db: Db):
        return Editor(tracker, db).publish(campaign_id, stream_id, body.version)

    @app.post("/api/campaigns/{campaign_id}/streams/{stream_id}/draft/cancel")
    def cancel_draft(campaign_id: ResourceId, stream_id: ResourceId, body: VersionInput, db: Db):
        return Editor(tracker, db).cancel(campaign_id, stream_id, body.version)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
