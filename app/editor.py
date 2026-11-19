import json
from copy import deepcopy
from datetime import UTC
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import Field, PositiveInt, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns import VersionInput, require_offer
from app.database import StreamEdit, StreamPublication
from app.errors import AppError
from app.tracker import Keitaro

HISTORY_PAGE_SIZE = 20


class DraftChange(VersionInput):
    action: Literal["add", "remove", "restore", "pin", "unpin"]
    offer_id: PositiveInt
    share: Annotated[int, Field(ge=1, le=100, strict=True)] | None = None

    @model_validator(mode="after")
    def check_share(self):
        if (self.share is not None) != (self.action == "pin"):
            raise ValueError("Для фиксации доли укажите целое число от 1 до 100")
        return self


class DraftConflict(AppError):
    def __init__(self, message: str, view: dict):
        super().__init__(409, message)
        self.view = view


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


def draft_baseline(edit: StreamEdit, live: list[dict], current: str) -> list[dict] | None:
    if edit.base_offers is not None:
        return edit.base_offers
    if edit.base == current:
        return visible_rows(edit, live)
    if edit.base == edit.published_signature:
        return edit.published
    return None


def offer_changes(before: list[dict], after: list[dict], *, include_pins: bool) -> list[dict]:
    fields = ("share", "state", "pinned") if include_pins else ("share", "state")
    offer_states = [
        {
            row["offer_id"]: {field: row[field] for field in fields}
            for row in snapshot
            if not row["removed"]
        }
        for snapshot in (before, after)
    ]
    original, modified = offer_states
    return [
        {"offer_id": offer_id, "before": original.get(offer_id), "after": modified.get(offer_id)}
        for offer_id in dict.fromkeys([*original, *modified])
        if original.get(offer_id) != modified.get(offer_id)
    ]


def editor_view(edit: StreamEdit | None, live: list[dict]) -> dict:
    version = edit.version if edit is not None else 0
    if edit is None or edit.draft is None:
        return {
            "version": version,
            "dirty": False,
            "stale": False,
            "offers": visible_rows(edit, live),
        }

    current = signature(live)
    already_applied = edit.base != current and signature(edit.draft) == current
    stale = edit.base != current and not already_applied
    baseline = draft_baseline(edit, live, current)
    return {
        "version": version,
        "dirty": True,
        "stale": stale,
        "offers": edit.draft,
        "preview": {
            "baseline_known": baseline is not None,
            "already_applied": already_applied,
            "changes": offer_changes(
                baseline if baseline is not None else live, edit.draft, include_pins=True
            ),
            "external_changes": offer_changes(baseline, live, include_pins=False)
            if stale and baseline is not None
            else [],
        },
    }


def publication_view(publication: StreamPublication) -> dict:
    applied_at = publication.applied_at
    if applied_at.tzinfo is None:
        applied_at = applied_at.replace(tzinfo=UTC)
    return {
        "id": publication.id,
        "applied_at": applied_at.isoformat(),
        "changes": offer_changes(
            publication.before_offers, publication.after_offers, include_pins=True
        )
        if publication.before_offers is not None
        else None,
    }


def restore_blocker(
    edit: StreamEdit | None, live: list[dict], publication: StreamPublication
) -> str | None:
    if edit is None or publication.before_offers is None:
        return "Для этого применения недоступен снимок предыдущего распределения."
    if edit.draft is not None:
        return "Примените или отмените открытый черновик перед возвратом."
    if signature(live) != signature(publication.after_offers):
        return "После последнего применения офферы изменились в Keitaro. Возврат заблокирован."
    return None


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

    def publications(self, campaign_id: int, stream_id: int):
        return (
            select(StreamPublication)
            .where(
                StreamPublication.tracker == self.scope,
                StreamPublication.campaign_id == campaign_id,
                StreamPublication.stream_id == stream_id,
            )
            .order_by(StreamPublication.id.desc())
        )

    def history(self, campaign_id: int, stream_id: int, before_id: int | None) -> dict:
        stream, edit = self.read(campaign_id, stream_id)
        query = self.publications(campaign_id, stream_id)
        if before_id is not None:
            query = query.where(StreamPublication.id < before_id)
        records = self.db.scalars(query.limit(HISTORY_PAGE_SIZE + 1)).all()
        page = records[:HISTORY_PAGE_SIZE]
        restore = None
        if page and before_id is None:
            restore = {
                "id": page[0].id,
                "reason": restore_blocker(edit, live_offers(stream), page[0]),
            }
        return {
            "entries": [publication_view(record) for record in page],
            "restore": restore,
            "next_before_id": page[-1].id if len(records) > HISTORY_PAGE_SIZE else None,
        }

    def restore(self, campaign_id: int, stream_id: int, publication_id: int, version: int) -> dict:
        stream, edit = self.read(campaign_id, stream_id)
        self.expect_version(edit, version)
        latest = self.db.scalar(self.publications(campaign_id, stream_id).limit(1))
        if latest is None or latest.id != publication_id:
            raise AppError(409, "Возврат доступен для последнего применения в этом потоке.")
        live = live_offers(stream)
        reason = restore_blocker(edit, live, latest)
        if reason is not None:
            raise DraftConflict(reason, editor_view(edit, live))

        active_ids = {row["offer_id"] for row in live if row["state"] == "active"}
        for row in latest.before_offers:
            if (
                row["state"] == "active"
                and not row["removed"]
                and row["offer_id"] not in active_ids
            ):
                require_offer(self.tracker, row["offer_id"])

        edit.base = signature(live)
        edit.base_offers = visible_rows(edit, live)
        edit.draft = deepcopy(latest.before_offers)
        self.db.commit()
        return editor_view(edit, live)

    def change(self, campaign_id: int, stream_id: int, change: DraftChange) -> dict:
        stream, edit = self.read(campaign_id, stream_id)
        self.expect_version(edit, change.version)
        live = live_offers(stream)
        base = signature(live)
        if edit is None:
            edit = StreamEdit(tracker=self.scope, campaign_id=campaign_id, stream_id=stream_id)
            self.db.add(edit)
        if edit.draft is not None and edit.base != base:
            raise DraftConflict(
                "Офферы изменились в Keitaro. Примените уже отправленный черновик или отмените его.",
                editor_view(edit, live),
            )
        if edit.draft is None:
            rows = visible_rows(edit, live)
            edit.base_offers = deepcopy(rows)
        else:
            rows = deepcopy(edit.draft)
            if edit.base_offers is None:
                edit.base_offers = draft_baseline(edit, live, base)
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
            raise DraftConflict(
                "Офферы изменились в Keitaro. Отмените черновик и повторите правки.",
                editor_view(edit, live),
            )

        before_offers = draft_baseline(edit, live, current)
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

        if before_offers is None or offer_changes(before_offers, edit.draft, include_pins=True):
            self.db.add(
                StreamPublication(
                    tracker=self.scope,
                    campaign_id=campaign_id,
                    stream_id=stream_id,
                    before_offers=before_offers,
                    after_offers=edit.draft,
                )
            )
        edit.published = edit.draft
        edit.published_signature = desired
        edit.base = None
        edit.base_offers = None
        edit.draft = None
        self.db.commit()
        return editor_view(edit, live)

    def cancel(self, campaign_id: int, stream_id: int, version: int) -> dict:
        stream, edit = self.read(campaign_id, stream_id)
        self.expect_version(edit, version)
        if edit is not None and edit.draft is not None:
            edit.base = None
            edit.base_offers = None
            edit.draft = None
            self.db.commit()
        return editor_view(edit, live_offers(stream))
