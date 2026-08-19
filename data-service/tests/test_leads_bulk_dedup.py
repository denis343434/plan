from fastapi.testclient import TestClient


def _lead(external_id: str) -> dict:
    return {
        "platform": "vk",
        "external_id": external_id,
        "group_url": f"https://vk.com/{external_id}",
    }


def test_duplicate_within_batch_is_skipped(client: TestClient) -> None:
    batch = [_lead("1"), _lead("1"), _lead("2")]
    resp = client.post("/leads/bulk", json=batch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["inserted"] == 2
    assert body["skipped"] == 1
    assert len(body["lead_ids"]) == 2

    listed = client.get("/leads").json()
    assert len(listed) == 2


def test_resubmitting_existing_leads_inserts_nothing(client: TestClient) -> None:
    first = client.post("/leads/bulk", json=[_lead("10"), _lead("11")])
    assert first.json()["inserted"] == 2

    second = client.post("/leads/bulk", json=[_lead("10"), _lead("11"), _lead("12")])
    body = second.json()
    assert body["inserted"] == 1
    assert body["skipped"] == 2

    listed = client.get("/leads").json()
    assert len(listed) == 3
    external_ids = {lead["external_id"] for lead in listed}
    assert external_ids == {"10", "11", "12"}
