from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient


def _create_account(client: TestClient, login: str) -> str:
    resp = client.post(
        "/accounts",
        json={
            "platform": "vk",
            "login": login,
            "purpose": "messaging",
            "hourly_limit": 100,
            "daily_limit": 1000,
        },
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def test_exactly_n_accounts_win_under_concurrent_requests(client: TestClient) -> None:
    account_ids = {_create_account(client, f"acc-{i}") for i in range(3)}

    def call_next_available(_: int):
        return client.post(
            "/accounts/next-available", json={"platform": "vk", "purpose": "messaging"}
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(call_next_available, range(10)))

    successes = [r for r in responses if r.status_code == 200]
    conflicts = [r for r in responses if r.status_code == 409]

    assert len(successes) == 3
    assert len(conflicts) == 7

    won_ids = {r.json()["id"] for r in successes}
    assert won_ids == account_ids
