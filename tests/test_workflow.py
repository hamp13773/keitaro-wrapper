import json

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import Settings
from app.demo import DemoKeitaro
from app.main import create_app
from app.tracker import GOOGLE_URL

EDITOR = "/api/campaigns/1/streams/2"
HISTORY = f"{EDITOR}/history"
CREATE = {
    "name": "test-summer",
    "country": "de",
    "offer_id": 101,
    "domain_id": 11,
    "group_id": 21,
    "traffic_source_id": 31,
}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    database = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("KEITARO_MODE", "demo")
    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(database_url=database, campaign_prefix="test_karpov_", keitaro_mode="demo")
    return settings, DemoKeitaro(settings.campaign_prefix)


@pytest.fixture
def client(environment):
    settings, demo = environment
    with TestClient(create_app(settings, httpx.MockTransport(demo.handle))) as app:
        yield app


def ok(response):
    assert response.is_success, response.text
    return response.json()


def change(client, view, action, offer_id, **fields):
    return ok(
        client.post(
            f"{EDITOR}/draft",
            json={
                "version": view["version"],
                "action": action,
                "offer_id": offer_id,
                **fields,
            },
        )
    )


def shares(view):
    return {offer["offer_id"]: offer["share"] for offer in view["offers"]}


def test_campaign_is_created_with_verified_order_and_metadata(client, environment):
    _, demo = environment
    response = client.post("/api/campaigns", json=CREATE)
    assert response.status_code == 201, response.text
    campaign = response.json()
    assert campaign["name"] == "test_karpov_test-summer"
    assert campaign["state"] == "active"
    assert [campaign[field] for field in ("domain_id", "group_id", "traffic_source_id")] == [
        11,
        21,
        31,
    ]
    detail = ok(client.get(f"/api/campaigns/{campaign['id']}"))
    google, fallback = detail["streams"]
    assert [google["position"], fallback["position"]] == [1, 2]
    assert (google["type"], google["countries"], google["destination"]) == (
        "regular",
        ["DE"],
        GOOGLE_URL,
    )
    assert demo.streams[google["id"]]["filters"] == [
        {"mode": "accept", "name": "country", "payload": ["DE"]}
    ]
    assert fallback["type"] == "default"
    assert demo.streams[fallback["id"]]["action_type"] == "do_nothing"
    assert demo.streams[fallback["id"]]["offers"] == [
        {"offer_id": 101, "state": "active", "share": 100}
    ]
    assert client.post("/api/campaigns", json=CREATE).status_code == 409
    assert client.post("/api/campaigns", json={**CREATE, "country": "ZZ"}).status_code == 422
    assert client.post("/api/campaigns", json={**CREATE, "group_id": 99}).status_code == 422


def test_failed_second_flow_archives_incomplete_campaign(environment):
    settings, demo = environment

    def fail_second_flow(request):
        if request.method == "POST" and request.url.path.endswith("/streams"):
            if json.loads(request.content)["type"] == "default":
                return httpx.Response(422)
        return demo.handle(request)

    with TestClient(create_app(settings, httpx.MockTransport(fail_second_flow))) as client:
        response = client.post("/api/campaigns", json=CREATE)
        assert response.status_code == 502
        assert len(ok(client.get("/api/campaigns"))) == 1
    created = next(row for row in demo.campaigns.values() if row["id"] != 1)
    assert created["state"] == "deleted"


def test_editor_draft_rounds_publishes_restores_and_cancels(client, environment):
    _, demo = environment
    before = ok(client.get(EDITOR))
    assert "preview" not in before
    draft = change(client, before, "add", 104)
    assert shares(draft) == {101: 25, 102: 25, 103: 25, 104: 25}
    assert [
        (row["offer_id"], row["before"]["share"] if row["before"] else None, row["after"]["share"])
        for row in draft["preview"]["changes"]
    ] == [(101, 34, 25), (102, 33, 25), (103, 33, 25), (104, None, 25)]
    draft = change(client, draft, "pin", 101, share=25)
    assert draft["preview"]["changes"][0]["after"]["pinned"] == 25
    draft = change(client, draft, "remove", 104)
    assert shares(draft) == {101: 25, 102: 38, 103: 37, 104: 0}
    assert [(row["offer_id"], row["share"]) for row in demo.streams[2]["offers"]] == [
        (101, 34),
        (102, 33),
        (103, 33),
    ]
    published = ok(client.post(f"{EDITOR}/draft/publish", json={"version": draft["version"]}))
    assert published["dirty"] is False
    assert "preview" not in published
    assert shares(published) == shares(draft)
    assert [(row["offer_id"], row["share"]) for row in demo.streams[2]["offers"]] == [
        (101, 25),
        (102, 38),
        (103, 37),
    ]
    draft = change(client, published, "restore", 104)
    assert shares(draft) == {101: 25, 102: 25, 103: 25, 104: 25}
    cancelled = ok(client.post(f"{EDITOR}/draft/cancel", json={"version": draft["version"]}))
    assert shares(cancelled) == shares(published)
    assert cancelled["dirty"] is False
    stats = ok(client.get(f"{EDITOR}/stats"))
    assert stats["offers"]["101"] == {"clicks": 128, "conversions": 9}


def test_published_changes_keep_unrelated_stream_settings(client, environment):
    _, demo = environment
    demo.streams[2]["comments"] = "Existing stream settings"
    demo.streams[2]["landings"] = [{"landing_id": 500, "share": 100}]
    draft = change(client, ok(client.get(EDITOR)), "add", 105)
    published = ok(client.post(f"{EDITOR}/draft/publish", json={"version": draft["version"]}))
    assert shares(published) == {101: 25, 102: 25, 103: 25, 105: 25}
    assert demo.streams[2]["landings"] == [{"landing_id": 500, "share": 100}]
    assert demo.streams[2]["comments"] == "Existing stream settings"


def test_editor_rejects_outdated_versions_and_external_edits(client, environment):
    _, demo = environment
    before = ok(client.get(EDITOR))
    draft = change(client, before, "remove", 101)
    stale = client.post(
        f"{EDITOR}/draft", json={"version": before["version"], "action": "remove", "offer_id": 102}
    )
    assert stale.status_code == 409
    demo.streams[2]["offers"][0]["share"] = 42
    demo.streams[2]["offers"][1]["share"] = 25
    response = client.post(f"{EDITOR}/draft/publish", json={"version": draft["version"]})
    assert response.status_code == 409
    conflict = response.json()["editor"]
    assert conflict == ok(client.get(EDITOR))
    assert conflict["stale"] is True
    assert conflict["preview"]["changes"][0] == {
        "offer_id": 101,
        "before": {"share": 34, "state": "active", "pinned": None},
        "after": None,
    }
    assert conflict["preview"]["external_changes"] == [
        {
            "offer_id": 101,
            "before": {"share": 34, "state": "active"},
            "after": {"share": 42, "state": "active"},
        },
        {
            "offer_id": 102,
            "before": {"share": 33, "state": "active"},
            "after": {"share": 25, "state": "active"},
        },
    ]
    current = ok(client.post(f"{EDITOR}/draft/cancel", json={"version": draft["version"]}))
    assert current["dirty"] is False
    assert shares(current)[101] == 42


def test_draft_survives_app_restart(environment):
    settings, demo = environment
    with TestClient(create_app(settings, httpx.MockTransport(demo.handle))) as client:
        draft = change(client, ok(client.get(EDITOR)), "remove", 102)
    with TestClient(create_app(settings, httpx.MockTransport(demo.handle))) as client:
        assert ok(client.get(EDITOR)) == draft


def test_migrated_draft_uses_live_values_if_original_snapshot_is_unavailable(client, environment):
    _, demo = environment
    original = change(client, ok(client.get(EDITOR)), "add", 104)
    config = Config("alembic.ini")
    command.downgrade(config, "0001_stream_edits")
    command.upgrade(config, "head")

    assert ok(client.get(EDITOR)) == original
    demo.streams[2]["offers"][0]["share"] = 42
    demo.streams[2]["offers"][1]["share"] = 25
    migrated = ok(client.get(EDITOR))
    assert migrated["stale"] is True
    assert migrated["offers"] == original["offers"]
    preview = migrated["preview"]
    assert preview["baseline_known"] is False
    assert preview["external_changes"] == []
    assert preview["changes"][0]["before"]["share"] == 42
    assert preview["changes"][0]["after"]["share"] == 25


def test_publication_history_restores_previous_distribution_through_draft(client, environment):
    _, demo = environment
    assert ok(client.get(HISTORY))["entries"] == []
    draft = change(client, ok(client.get(EDITOR)), "pin", 101, share=34)
    pinned = ok(client.post(f"{EDITOR}/draft/publish", json={"version": draft["version"]}))
    removed = change(client, pinned, "remove", 101)
    published = ok(client.post(f"{EDITOR}/draft/publish", json={"version": removed["version"]}))

    history = ok(client.get(HISTORY))
    newest, older = history["entries"]
    assert newest["applied_at"].endswith("+00:00")
    assert history["restore"] == {"id": newest["id"], "reason": None}
    assert newest["changes"][0] == {
        "offer_id": 101,
        "before": {"share": 34, "state": "active", "pinned": 34},
        "after": None,
    }
    older_rejected = client.post(
        f"{HISTORY}/{older['id']}/restore", json={"version": published["version"]}
    )
    assert older_rejected.status_code == 409

    path = f"{HISTORY}/{newest['id']}/restore"
    restored = ok(client.post(path, json={"version": published["version"]}))
    assert restored["dirty"] is True
    assert shares(restored) == {101: 34, 102: 33, 103: 33}
    assert restored["preview"]["changes"][-1]["after"]["pinned"] == 34
    assert 101 not in [row["offer_id"] for row in demo.streams[2]["offers"]]
    assert len(ok(client.get(HISTORY))["entries"]) == 2
    assert client.post(path, json={"version": restored["version"]}).status_code == 409

    done = ok(client.post(f"{EDITOR}/draft/publish", json={"version": restored["version"]}))
    assert done["dirty"] is False
    assert next(row for row in demo.streams[2]["offers"] if row["offer_id"] == 101)["share"] == 34
    assert len(ok(client.get(HISTORY))["entries"]) == 3


def test_external_edits_block_restoration_and_subsequent_application(client, environment):
    _, demo = environment
    draft = change(client, ok(client.get(EDITOR)), "add", 104)
    published = ok(client.post(f"{EDITOR}/draft/publish", json={"version": draft["version"]}))
    entry = ok(client.get(HISTORY))["entries"][0]
    restore_path = f"{HISTORY}/{entry['id']}/restore"
    demo.streams[2]["offers"][0]["share"] = 26
    demo.streams[2]["offers"][1]["share"] = 24
    assert "изменились в Keitaro" in ok(client.get(HISTORY))["restore"]["reason"]
    assert client.post(restore_path, json={"version": published["version"]}).status_code == 409
    assert ok(client.get(EDITOR))["dirty"] is False

    demo.streams[2]["offers"][0]["share"] = 25
    demo.streams[2]["offers"][1]["share"] = 25
    restored = ok(client.post(restore_path, json={"version": published["version"]}))
    demo.streams[2]["offers"][0]["share"] = 26
    demo.streams[2]["offers"][1]["share"] = 24
    blocked = client.post(f"{EDITOR}/draft/publish", json={"version": restored["version"]})
    assert blocked.status_code == 409
    assert blocked.json()["editor"]["stale"] is True
    assert demo.streams[2]["offers"][0]["share"] == 26
    assert len(ok(client.get(HISTORY))["entries"]) == 1


def test_history_paginates_from_latest_without_repeats(client):
    view = ok(client.get(EDITOR))
    for _ in range(11):
        for action, fields in (("pin", {"share": 34}), ("unpin", {})):
            draft = change(client, view, action, 101, **fields)
            view = ok(client.post(f"{EDITOR}/draft/publish", json={"version": draft["version"]}))
    first_page = ok(client.get(HISTORY))
    second_page = ok(client.get(HISTORY, params={"before_id": first_page["next_before_id"]}))
    ids = [entry["id"] for page in (first_page, second_page) for entry in page["entries"]]
    assert ids == sorted(set(ids), reverse=True)
    assert len(ids) == 22
    assert second_page["next_before_id"] is None
    assert second_page["restore"] is None


def test_retry_after_lost_publish_response_uses_persisted_remote_state(environment):
    settings, demo = environment
    puts = 0

    def lose_response(request):
        nonlocal puts
        response = demo.handle(request)
        if request.method == "PUT" and request.url.path.endswith("/streams/2"):
            puts += 1
            raise httpx.ReadTimeout("Response was lost after saving", request=request)
        return response

    with TestClient(create_app(settings, httpx.MockTransport(lose_response))) as client:
        draft = change(client, ok(client.get(EDITOR)), "add", 105)
        path = f"{EDITOR}/draft/publish"
        body = {"version": draft["version"]}
        assert client.post(path, json=body).status_code == 504
        lost = ok(client.get(EDITOR))
        assert lost["stale"] is False
        assert lost["preview"]["already_applied"] is True
        assert ok(client.get(HISTORY))["entries"] == []
        published = ok(client.post(path, json=body))
        assert puts == 1
        assert published["dirty"] is False
        assert shares(published) == shares(draft)
        [entry] = ok(client.get(HISTORY))["entries"]
        assert entry["changes"][0]["before"]["share"] == 34
        assert entry["changes"][0]["after"]["share"] == 25


def test_prefix_and_flow_ownership_are_enforced(client, environment):
    _, demo = environment
    demo.campaigns[8] = {**demo.campaigns[1], "id": 8, "name": "test_other_campaign"}
    demo.streams[8] = {**demo.streams[2], "id": 8, "campaign_id": 8}
    assert [row["id"] for row in ok(client.get("/api/campaigns"))] == [1]
    assert client.get("/api/campaigns/8").status_code == 404
    assert client.get("/api/campaigns/8/streams/8/history").status_code == 404
    assert (
        client.post(
            "/api/campaigns/1/streams/8/draft",
            json={"version": 0, "action": "remove", "offer_id": 101},
        ).status_code
        == 404
    )
    assert client.get("/api/campaigns/1/streams/1").status_code == 422


def test_live_campaign_links_to_its_admin_page(environment):
    settings, demo = environment
    live = Settings(
        keitaro_mode="live",
        keitaro_url="https://tracker.example.test/",
        keitaro_api_key="example-token",
        database_url=settings.database_url,
    )
    with TestClient(create_app(live, httpx.MockTransport(demo.handle))) as client:
        detail = ok(client.get("/api/campaigns/1"))
    assert (
        detail["campaign"]["admin_url"] == "https://tracker.example.test/admin/#!/campaign/1/main"
    )
