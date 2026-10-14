import asyncio

from fastapi.testclient import TestClient

from sportflex.web import app as app_module


class FakePool:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts

    def health(self) -> dict:
        return {"healthy": all(item["healthy"] for item in self.accounts), "accounts": self.accounts}


def _client(pool: FakePool) -> TestClient:
    app_module.app.state.pool = pool
    return TestClient(app_module.app)  # no `with`: the real lifespan would start Chrome


def test_health_is_ok_while_every_browser_runs():
    response = _client(FakePool([{"id": "me", "healthy": True, "error": ""}])).get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_health_is_503_when_a_browser_is_gone():
    pool = FakePool([{"id": "me", "healthy": True, "error": ""}, {"id": "family", "healthy": False, "error": "壞了"}])
    response = _client(pool).get("/api/health")
    assert response.status_code == 503
    assert response.json()["accounts"][1]["error"] == "壞了"


def test_watchdog_terminates_once_an_account_dies(capsys):
    accounts = [{"id": "me", "healthy": True, "error": ""}]
    pool = FakePool(accounts)
    calls = []

    def terminate():
        calls.append(1)

    async def run():
        task = asyncio.create_task(app_module._exit_when_dead(pool, terminate, interval=0.01))
        await asyncio.sleep(0.05)
        assert calls == []
        accounts[0].update(healthy=False, error="瀏覽器重開 5 次都失敗")
        await asyncio.wait_for(task, 1)

    asyncio.run(run())
    assert calls == [1]
    assert "me（瀏覽器重開 5 次都失敗）" in capsys.readouterr().out
