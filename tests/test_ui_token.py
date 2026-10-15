import pytest
from fastapi.testclient import TestClient

from sportflex.web import app as app_module


class FakePool:
    def meta(self) -> dict:
        return {"instance": "test", "venues": [], "accounts": []}

    def health(self) -> dict:
        return {"healthy": True, "accounts": []}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module.app.state, "pool", FakePool(), raising=False)
    monkeypatch.setattr(app_module.app.state, "ui_token", "s3cret", raising=False)
    return TestClient(app_module.app)


def test_api_without_token_is_refused(client):
    response = client.get("/api/meta")
    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "需要 token"}


def test_page_without_token_explains_how_to_get_in(client):
    response = client.get("/")
    assert response.status_code == 401
    assert "?token=" in response.text


def test_token_link_sets_a_cookie_that_the_page_and_api_reuse(client):
    response = client.get("/?token=s3cret", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/meta").status_code == 200
    assert client.get("/").status_code == 200


def test_bearer_header_works_and_a_wrong_token_does_not(client):
    assert client.get("/api/meta", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/api/meta", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/meta?token=nope").status_code == 401


def test_health_stays_open_for_the_docker_check(client):
    assert client.get("/api/health").status_code == 200


def test_no_token_configured_leaves_everything_open(client, monkeypatch):
    monkeypatch.setattr(app_module.app.state, "ui_token", "")
    assert client.get("/api/meta").status_code == 200
