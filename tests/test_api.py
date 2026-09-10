import os
import tempfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.api import make_api_subapp
from app.stores import (ApiTokenStore, AuditStore, ChannelStore,
                        ConnectionStore, RecordingStore, SettingsStore, ViewerStore)


@pytest.fixture
async def api_client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    stores = {
        "viewers": ViewerStore(path), "channels": ChannelStore(path),
        "connections": ConnectionStore(path), "api_tokens": ApiTokenStore(path),
        "audit": AuditStore(path), "settings": SettingsStore(path),
        "recordings": RecordingStore(path),
    }
    for store in stores.values():
        store.init_schema()
    token, _ = await stores["api_tokens"].create("test", "tests")
    root = web.Application()
    root.add_subapp("/api/v1", make_api_subapp(stores))
    server = TestServer(root)
    client = TestClient(server)
    await client.start_server()
    client.api_token = token
    try:
        yield client
    finally:
        await client.close()
        os.unlink(path)


class TestApiAuth:
    async def test_missing_token_is_401(self, api_client):
        response = await api_client.get("/api/v1/users")
        assert response.status == 401

    async def test_invalid_token_is_401(self, api_client):
        response = await api_client.get("/api/v1/users", headers={"Authorization": "Bearer bad"})
        assert response.status == 401

    async def test_valid_token_works(self, api_client):
        response = await api_client.get("/api/v1/users", headers={"Authorization": f"Bearer {api_client.api_token}"})
        assert response.status == 200

    async def test_api_info_is_public(self, api_client):
        response = await api_client.get("/api/v1/")
        assert response.status == 200
        assert (await response.json())["version"] == "1"


class TestApiUsers:
    async def test_create_does_not_return_existing_secret(self, api_client):
        headers = {"Authorization": f"Bearer {api_client.api_token}"}
        response = await api_client.post("/api/v1/users", headers=headers,
                                         json={"name": "Alice"})
        assert response.status == 201
        body = await response.json()
        assert body["viewer_token"]
        assert "token" not in body
        user_id = body["id"]
        response = await api_client.get(f"/api/v1/users/{user_id}", headers=headers)
        assert response.status == 200
        assert "token" not in await response.json()

    async def test_pagination(self, api_client):
        headers = {"Authorization": f"Bearer {api_client.api_token}"}
        for name in ("A", "B", "C"):
            await api_client.post("/api/v1/users", headers=headers, json={"name": name})
        response = await api_client.get("/api/v1/users?page=1&per_page=2", headers=headers)
        body = await response.json()
        assert len(body["items"]) == 2
        assert body["pagination"]["total"] == 3


class TestApiRecordingStorage:
    async def test_storage_endpoint(self, api_client):
        headers = {"Authorization": f"Bearer {api_client.api_token}"}
        response = await api_client.get("/api/v1/storage", headers=headers)
        assert response.status == 200
        body = await response.json()
        assert body["reserve_bytes"] == 10 * 1024 ** 3

    async def test_recording_rejects_bad_time(self, api_client):
        headers = {"Authorization": f"Bearer {api_client.api_token}"}
        response = await api_client.post("/api/v1/recordings", headers=headers,
                                         json={"viewer_id": 1, "channel_id": 1,
                                               "start_at": "bad", "end_at": "bad"})
        assert response.status == 422
