import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import truststore

from app.config import Settings
from app.errors import AppError

CATALOG_PATHS = {
    "domains": "domains",
    "groups": "groups?type=campaigns",
    "sources": "traffic_sources",
    "offers": "offers",
}
GOOGLE_URL = "https://www.google.com/"


class Keitaro:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        origin = settings.keitaro_url or "https://demo.invalid"
        self.client = httpx.Client(
            base_url=f"{origin}/admin_api/v1/",
            headers={
                "Api-Key": settings.keitaro_api_key.get_secret_value(),
                "Accept": "application/json",
            },
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            timeout=httpx.Timeout(12, connect=6),
            transport=transport,
        )

    def request(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise AppError(504, "Keitaro не ответил вовремя. Обновите данные и повторите.") from exc
        except httpx.RequestError as exc:
            raise AppError(502, "Не удалось установить соединение с Keitaro.") from exc

        status = response.status_code
        if status == 404:
            raise AppError(404, "Объект не найден в Keitaro.")
        if status in (401, 402, 403):
            raise AppError(
                502, f"Keitaro не разрешил запрос ({status}). Проверьте ключ и права Admin API."
            )
        if status >= 400 or response.is_redirect:
            raise AppError(
                502, f"Keitaro отклонил запрос {method} /{path.split('?')[0]} ({status})."
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise AppError(502, "Keitaro вернул ответ в неожиданном формате.") from exc

    def catalogs(self) -> dict[str, list[dict]]:
        with ThreadPoolExecutor(max_workers=4) as pool:
            catalogs = dict(zip(CATALOG_PATHS, pool.map(self.catalog, CATALOG_PATHS), strict=True))
        return catalogs

    def catalog(self, kind: str) -> list[dict]:
        rows = self.request("GET", CATALOG_PATHS[kind])
        if not isinstance(rows, list):
            raise AppError(502, "Keitaro вернул каталог в неожиданном формате.")
        result = []
        for row in rows:
            if row.get("state") in ("deleted", "disabled", "archived"):
                continue
            if kind == "groups" and row.get("type") not in (None, "campaigns"):
                continue
            name = row.get("name") or row.get("domain") or row.get("url")
            result.append({"id": int(row["id"]), "name": name or f"#{row['id']}"})
        return sorted(result, key=lambda row: (row["name"].casefold(), row["id"]))

    def campaigns(self) -> list[dict]:
        results = []
        for offset in range(0, 100_000, 500):
            page = self.request("GET", "campaigns", params={"offset": offset, "limit": 500})
            if not isinstance(page, list):
                raise AppError(502, "Keitaro вернул кампании в неожиданном формате.")
            results.extend(row for row in page if self.is_editable_campaign(row))
            if len(page) < 500:
                return sorted(results, key=lambda row: int(row["id"]), reverse=True)
        raise AppError(502, "Каталог кампаний превышает лимит загрузки.")

    def is_editable_campaign(self, campaign: dict) -> bool:
        return campaign.get("state") != "deleted" and campaign["name"].startswith(
            self.settings.campaign_prefix
        )

    def campaign(self, campaign_id: int) -> dict:
        campaign = self.request("GET", f"campaigns/{campaign_id}")
        if not self.is_editable_campaign(campaign):
            raise AppError(404, "Кампания с разрешённым префиксом не найдена.")
        return campaign

    def streams(self, campaign_id: int) -> list[dict]:
        return sorted(
            self.request("GET", f"campaigns/{campaign_id}/streams"),
            key=lambda stream: (int(stream.get("position") or 0), int(stream["id"])),
        )

    def stream(self, campaign_id: int, stream_id: int) -> dict:
        stream = self.request("GET", f"streams/{stream_id}")
        if int(stream["campaign_id"]) != campaign_id or stream.get("state") == "deleted":
            raise AppError(404, "Поток не найден в этой кампании.")
        if stream.get("schema") != "landings":
            raise AppError(422, "У этого потока нет схемы с офферами.")
        return stream

    def stats(self, campaign_id: int, stream_id: int) -> dict:
        timezone = self.settings.report_timezone
        today = datetime.now(ZoneInfo(timezone)).date()
        start = today - timedelta(days=6)
        report = self.request(
            "POST",
            "report/build",
            json={
                "range": {"from": start.isoformat(), "to": today.isoformat(), "timezone": timezone},
                "grouping": ["offer_id"],
                "metrics": ["clicks", "conversions"],
                "filters": [
                    {"name": "campaign_id", "operator": "EQUALS", "expression": campaign_id},
                    {"name": "stream_id", "operator": "EQUALS", "expression": stream_id},
                ],
                "limit": 0,
            },
        )
        rows = {
            int(row["offer_id"]): {
                "clicks": int(row["clicks"]),
                "conversions": int(row["conversions"]),
            }
            for row in report["rows"]
            if row.get("offer_id")
        }
        return {"from": start, "to": today, "timezone": timezone, "offers": rows}
