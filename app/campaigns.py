import re
from hashlib import sha256
from typing import Annotated

import pycountry
from pydantic import BaseModel, Field, PositiveInt, StringConstraints, field_validator

from app.errors import AppError
from app.tracker import GOOGLE_URL, Keitaro


class CampaignInput(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    country: Annotated[
        str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{2}$")
    ]
    offer_id: PositiveInt
    domain_id: PositiveInt
    group_id: PositiveInt
    traffic_source_id: PositiveInt

    @field_validator("country")
    @classmethod
    def validate_country(cls, code: str) -> str:
        if pycountry.countries.get(alpha_2=code) is None:
            raise ValueError("Введите двухбуквенный код страны ISO 3166-1")
        return code


def campaign_summary(raw: dict) -> dict:
    return {
        field: raw.get(field)
        for field in (
            "id",
            "name",
            "state",
            "alias",
            "domain_id",
            "group_id",
            "traffic_source_id",
        )
    }


def stream_summary(raw: dict) -> dict:
    stream = {
        field: raw.get(field) for field in ("id", "name", "position", "type", "schema", "state")
    }
    if raw.get("schema") == "redirect" and isinstance(raw.get("action_payload"), str):
        stream["destination"] = raw["action_payload"]
    stream["countries"] = []
    for rule in raw.get("filters") or []:
        if rule["name"] == "country" and rule["mode"] == "accept":
            payload = rule.get("payload")
            if isinstance(payload, list):
                stream["countries"].extend(payload)
    return stream


def require_offer(tracker: Keitaro, offer_id: int) -> None:
    offer = tracker.request("GET", f"offers/{offer_id}")
    if offer.get("state", "active") != "active":
        raise AppError(422, "Выбранный оффер недоступен для добавления.")


def create_fallback_stream(tracker: Keitaro, campaign_id: int, offer_id: int) -> None:
    tracker.request(
        "POST",
        "streams",
        json={
            "campaign_id": campaign_id,
            "name": "Остальные страны → офферы",
            "type": "default",
            "position": 2,
            "schema": "landings",
            "action_type": "do_nothing",
            "state": "active",
            "collect_clicks": True,
            "offers": [{"offer_id": offer_id, "state": "active", "share": 100}],
        },
    )


def create_campaign(tracker: Keitaro, data: CampaignInput) -> dict:
    prefix = tracker.settings.campaign_prefix
    name = data.name if data.name.startswith(prefix) else prefix + data.name
    if not name.removeprefix(prefix).strip():
        raise AppError(422, "Введите название после префикса кампании.")
    if len(name) > 100:
        raise AppError(422, "Название вместе с префиксом должно быть не длиннее 100 символов.")
    if any(ord(char) < 32 for char in name):
        raise AppError(422, "Название кампании должно занимать одну строку.")

    catalogs = tracker.catalogs()
    for kind, item_id in (
        ("domains", data.domain_id),
        ("groups", data.group_id),
        ("sources", data.traffic_source_id),
        ("offers", data.offer_id),
    ):
        if not any(item["id"] == item_id for item in catalogs[kind]):
            raise AppError(422, "Выбранная настройка или оффер недоступны. Обновите справочники.")

    slug = re.sub(r"[^a-z0-9_]+", "-", name.lower()).strip("-")[:38]
    alias = f"{slug}-{sha256(name.encode()).hexdigest()[:8]}"
    if any(row["name"] == name or row.get("alias") == alias for row in tracker.campaigns()):
        raise AppError(409, "Кампания с таким названием уже существует.")

    choices = {
        "group_id": data.group_id,
        "domain_id": data.domain_id,
        "traffic_source_id": data.traffic_source_id,
    }
    campaign = tracker.request(
        "POST",
        "campaigns",
        json={
            "name": name,
            "alias": alias,
            "type": "position",
            "state": "disabled",
            **choices,
        },
    )
    campaign_id = int(campaign["id"])
    try:
        tracker.request(
            "POST",
            "streams",
            json={
                "campaign_id": campaign_id,
                "name": f"{data.country} → Google",
                "type": "regular",
                "position": 1,
                "schema": "redirect",
                "action_type": "http",
                "action_payload": GOOGLE_URL,
                "state": "active",
                "collect_clicks": True,
                "filters": [{"name": "country", "mode": "accept", "payload": [data.country]}],
            },
        )
        create_fallback_stream(tracker, campaign_id, data.offer_id)
        configured = tracker.campaign(campaign_id)
        streams = tracker.streams(campaign_id)
        verify_creation(configured, choices, streams, data)
        tracker.request("POST", f"campaigns/{campaign_id}/enable")
        campaign = tracker.campaign(campaign_id)
        if campaign.get("state") != "active":
            raise AppError(502, "Keitaro не подтвердил включение кампании.")
    except AppError as exc:
        try:
            tracker.request("DELETE", f"campaigns/{campaign_id}")
        except AppError as cleanup:
            raise AppError(
                502, f"{exc.message} Проверьте кампанию #{campaign_id} в Keitaro."
            ) from cleanup
        raise exc
    return campaign_summary(campaign)


def verify_creation(campaign: dict, choices: dict, streams: list[dict], data: CampaignInput):
    if any(str(campaign.get(field)) != str(value) for field, value in choices.items()):
        raise AppError(502, "Keitaro не подтвердил домен, группу или источник кампании.")
    if len(streams) != 2 or any(stream.get("state") != "active" for stream in streams):
        raise AppError(502, "Keitaro не подтвердил создание двух активных потоков.")
    first, second = streams
    if (
        first.get("type") != "regular"
        or first.get("schema") != "redirect"
        or first.get("action_type") != "http"
        or first.get("action_payload") != GOOGLE_URL
        or stream_summary(first)["countries"] != [data.country]
        or second.get("type") != "default"
        or second.get("schema") != "landings"
        or second.get("filters")
    ):
        raise AppError(502, "Keitaro вернул настройки потоков, отличающиеся от запрошенных.")
    offers = second.get("offers") or []
    if len(offers) != 1 or (
        int(offers[0]["offer_id"]),
        int(offers[0]["share"]),
        offers[0]["state"],
    ) != (data.offer_id, 100, "active"):
        raise AppError(502, "Keitaro не подтвердил привязку оффера с долей 100%.")


class VersionInput(BaseModel):
    version: Annotated[int, Field(ge=0, strict=True)]
