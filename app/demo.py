import json
from itertools import count
from secrets import randbelow
from threading import Lock

import httpx

from app.tracker import GOOGLE_URL


class DemoKeitaro:
    """In-memory Admin API fixtures for the zero-credentials demo and tests."""

    def __init__(self, prefix: str):
        self.lock = Lock()
        self.ids = count(1_000 + randbelow(10_000_000))
        self.catalogs = {
            "domains": [{"id": 11, "name": "go.example.test", "state": "active"}],
            "groups": [{"id": 21, "name": "Тестовые кампании", "type": "campaigns"}],
            "traffic_sources": [{"id": 31, "name": "Тестовый источник", "state": "active"}],
            "offers": [
                {"id": 101, "name": "Atlas · Lead form", "state": "active"},
                {"id": 102, "name": "Harbor · Product trial", "state": "active"},
                {"id": 103, "name": "Fieldwork · Newsletter", "state": "active"},
                {"id": 104, "name": "Mosaic · Sign-up", "state": "active"},
                {"id": 105, "name": "Relay · Booking", "state": "active"},
            ],
        }
        self.campaigns = {
            1: {
                "id": 1,
                "name": prefix + "spring-launch",
                "alias": "spring-launch",
                "state": "active",
                "type": "position",
                "domain_id": 11,
                "group_id": 21,
                "traffic_source_id": 31,
            }
        }
        self.streams = {
            1: {
                "id": 1,
                "campaign_id": 1,
                "name": "PL → Google",
                "state": "active",
                "position": 1,
                "type": "regular",
                "schema": "redirect",
                "action_type": "http",
                "action_payload": GOOGLE_URL,
                "filters": [{"name": "country", "mode": "accept", "payload": ["PL"]}],
                "offers": [],
            },
            2: {
                "id": 2,
                "campaign_id": 1,
                "name": "Остальные страны → офферы",
                "state": "active",
                "position": 2,
                "type": "default",
                "schema": "landings",
                "filters": [],
                "offers": [
                    {"id": 1, "offer_id": 101, "state": "active", "share": 34},
                    {"id": 2, "offer_id": 102, "state": "active", "share": 33},
                    {"id": 3, "offer_id": 103, "state": "active", "share": 33},
                ],
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            return self.respond(request)

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/admin_api/v1/").split("/")
        method = request.method
        entity = path[0]
        item_id = int(path[1]) if len(path) > 1 and path[1].isdigit() else 0
        action = path[2] if len(path) > 2 else None
        body = json.loads(request.content) if request.content else {}
        if method == "GET" and entity in self.catalogs:
            catalog = self.catalogs[entity]
            if item_id:
                row = next((row for row in catalog if row["id"] == item_id), None)
                return httpx.Response(200, json=row) if row else httpx.Response(404)
            return httpx.Response(200, json=catalog)
        if entity == "campaigns":
            if method == "GET" and not item_id:
                rows = sorted(self.campaigns.values(), key=lambda row: row["id"], reverse=True)
                rows = [row for row in rows if row["state"] != "deleted"]
                offset = int(request.url.params.get("offset", 0))
                limit = int(request.url.params.get("limit", 500))
                return httpx.Response(200, json=rows[offset : offset + limit])
            if method == "POST" and not item_id:
                if any(
                    row["state"] != "deleted" and row["alias"] == body["alias"]
                    for row in self.campaigns.values()
                ):
                    return httpx.Response(409)
                row = {**body, "id": next(self.ids)}
                self.campaigns[row["id"]] = row
                return httpx.Response(200, json=row)
            if item_id in self.campaigns:
                row = self.campaigns[item_id]
                if method == "GET" and action == "streams":
                    return httpx.Response(
                        200,
                        json=[
                            stream
                            for stream in self.streams.values()
                            if stream["campaign_id"] == item_id
                        ],
                    )
                if method == "POST" and action == "enable":
                    row["state"] = "active"
                if method == "DELETE":
                    row["state"] = "deleted"
                return httpx.Response(200, json=row)
        if entity == "streams":
            if method == "POST" and not item_id:
                if not body.get("action_type"):
                    return httpx.Response(422, json={"action_type": ["Action Type is required"]})
                row = {"filters": [], "offers": [], **body, "id": next(self.ids)}
                self.streams[row["id"]] = row
                return httpx.Response(200, json=row)
            if item_id in self.streams:
                row = self.streams[item_id]
                if method == "PUT":
                    row["offers"] = [
                        {**offer, "id": offer["id"] if "id" in offer else next(self.ids)}
                        for offer in body["offers"]
                    ]
                return httpx.Response(200, json=row)
        if method == "POST" and path == ["report", "build"]:
            stream_id = next(
                row["expression"] for row in body["filters"] if row["name"] == "stream_id"
            )
            rows = (
                [
                    {"offer_id": 101, "clicks": 128, "conversions": 9},
                    {"offer_id": 102, "clicks": 117, "conversions": 7},
                    {"offer_id": 103, "clicks": 123, "conversions": 5},
                ]
                if stream_id == 2
                else []
            )
            return httpx.Response(200, json={"rows": rows})
        return httpx.Response(404)
