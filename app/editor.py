import json
from copy import deepcopy
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import Field, PositiveInt, model_validator
from sqlalchemy.orm import Session

from app.campaigns import VersionInput, require_offer
from app.database import StreamEdit
from app.errors import AppError
from app.tracker import Keitaro


class DraftChange(VersionInput):
    action: Literal["add", "remove", "restore", "pin", "unpin"]
    offer_id: PositiveInt
    share: Annotated[int, Field(ge=1, le=100, strict=True)] | None = None

    @model_validator(mode="after")
    def check_share(self):
        if (self.share is not None) != (self.action == "pin"):
            raise ValueError("Для фиксации доли укажите целое число от 1 до 100")
        return self


def live_offers(stream: dict) -> list[dict]:
    return [
        {
            "offer_id": int(row["offer_id"]),
            "share": int(row["share"]),
            "state": row.get("state") or "active",
            "removed": False,
            "pinned": None,
        }
        for row in stream.get("offers") or []
        if row.get("state") != "deleted"
    ]


def signature(rows: list[dict]) -> str:
    fields = sorted(
        (row["offer_id"], row["share"], row["state"]) for row in rows if not row["removed"]
    )
    return sha256(json.dumps(fields).encode()).hexdigest()


def visible_rows(edit: StreamEdit | None, live: list[dict]) -> list[dict]:
    if edit is not None and edit.published_signature == signature(live):
        return deepcopy(edit.published)
    return deepcopy(live)


def editor_view(edit: StreamEdit | None, live: list[dict]) -> dict:
    dirty = edit is not None and edit.draft is not None
    current = signature(live)
    return {
        "version": edit.version if edit is not None else 0,
        "dirty": dirty,
        "stale": bool(dirty and edit.base != current and signature(edit.draft) != current),
        "offers": edit.draft if dirty else visible_rows(edit, live),
    }


def recalculate(rows: list[dict]) -> None:
    active = [row for row in rows if not row["removed"] and row["state"] == "active"]
    if not active:
        return
    unpinned = [row for row in active if row["pinned"] is None]
    remaining = 100 - sum(row["pinned"] or 0 for row in active)
    if remaining < len(unpinned) or (not unpinned and remaining != 0):
        raise AppError(
            422,
            "Закреплённые доли не оставляют нужный процент для распределения. Измените или снимите фиксацию.",
        )
    share, leftover = divmod(remaining, len(unpinned)) if unpinned else (0, 0)
    for row in active:
        if row["pinned"] is not None:
            row["share"] = row["pinned"]
    for index, row in enumerate(unpinned):
        row["share"] = share + (index < leftover)


def change_rows(tracker: Keitaro, rows: list[dict], change: DraftChange) -> None:
    row = next((item for item in rows if item["offer_id"] == change.offer_id), None)
    if change.action in ("add", "restore"):
        if row is not None and not row["removed"]:
            raise AppError(409, "Этот оффер уже есть в потоке.")
        if row is None and change.action == "restore":
            raise AppError(404, "Удалённый оффер не найден.")
        require_offer(tracker, change.offer_id)
        if row is None:
            rows.append(
                {
                    "offer_id": change.offer_id,
                    "share": 0,
                    "state": "active",
                    "removed": False,
                    "pinned": None,
                }
            )
        else:
            row.update(removed=False, state="active", pinned=None)
    elif row is None or row["removed"]:
        raise AppError(404, "Оффер не найден в потоке.")
    elif change.action == "remove":
        row.update(removed=True, share=0, pinned=None)
    else:
        if row["state"] != "active":
            raise AppError(422, "Можно закрепить долю только активного оффера.")
        row["pinned"] = change.share
    recalculate(rows)


class Editor:
    def __init__(self, tracker: Keitaro, db: Session):
        self.tracker = tracker
        self.db = db
        settings = tracker.settings
        self.scope = sha256(
            f"{settings.keitaro_mode}:{settings.keitaro_url}:{settings.campaign_prefix}".encode()
        ).hexdigest()

    def read(self, campaign_id: int, stream_id: int) -> tuple[dict, StreamEdit | None]:
        self.tracker.campaign(campaign_id)
        stream = self.tracker.stream(campaign_id, stream_id)
        return stream, self.db.get(StreamEdit, (self.scope, campaign_id, stream_id))

    def expect_version(self, edit: StreamEdit | None, version: int):
        if version != (edit.version if edit is not None else 0):
            raise AppError(
                409, "Черновик изменился. Обновите поток, чтобы загрузить текущую версию."
            )

    def change(self, campaign_id: int, stream_id: int, change: DraftChange) -> dict:
        stream, edit = self.read(campaign_id, stream_id)
        self.expect_version(edit, change.version)
        live = live_offers(stream)
        base = signature(live)
        if edit is None:
            edit = StreamEdit(tracker=self.scope, campaign_id=campaign_id, stream_id=stream_id)
            self.db.add(edit)
        if edit.draft is not None and edit.base != base:
            raise AppError(
                409,
                "Офферы изменились в Keitaro. Примените уже отправленный черновик или отмените его.",
            )
        rows = deepcopy(edit.draft) if edit.draft is not None else visible_rows(edit, live)
        change_rows(self.tracker, rows, change)
        edit.base = base
        edit.draft = rows
        self.db.commit()
        return editor_view(edit, live)

    def publish(self, campaign_id: int, stream_id: int, version: int) -> dict:
        stream, edit = self.read(campaign_id, stream_id)
        self.expect_version(edit, version)
        live = live_offers(stream)
        if edit is None or edit.draft is None:
            return editor_view(edit, live)

        desired = signature(edit.draft)
        current = signature(live)
        if current != desired and current != edit.base:
            raise AppError(
                409, "Офферы изменились в Keitaro. Отмените черновик и повторите правки."
            )

        edit.version += 1
        self.db.flush()
        if current != desired:
            existing_ids = {
                int(row["offer_id"]): row.get("id") for row in stream.get("offers") or []
            }
            offers = []
            for row in edit.draft:
                if row["removed"]:
                    continue
                item = {key: row[key] for key in ("offer_id", "share", "state")}
                if existing_ids.get(row["offer_id"]):
                    item["id"] = existing_ids[row["offer_id"]]
                offers.append(item)
            self.tracker.request("PUT", f"streams/{stream_id}", json={"offers": offers})
            live = live_offers(self.tracker.stream(campaign_id, stream_id))
            if signature(live) != desired:
                raise AppError(
                    502,
                    "Keitaro вернул другие доли или офферы. Обновите поток и проверьте результат.",
                )

        edit.published = edit.draft
        edit.published_signature = desired
        edit.base = None
        edit.draft = None
        self.db.commit()
        return editor_view(edit, live)

    def cancel(self, campaign_id: int, stream_id: int, version: int) -> dict:
        stream, edit = self.read(campaign_id, stream_id)
        self.expect_version(edit, version)
        if edit is not None and edit.draft is not None:
            edit.base = None
            edit.draft = None
            self.db.commit()
        return editor_view(edit, live_offers(stream))
