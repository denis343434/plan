from fastapi.testclient import TestClient


def test_campaign_template_crud_flow(client: TestClient) -> None:
    campaign_resp = client.post(
        "/campaigns", json={"name": "Summer promo", "platform": "vk", "keyword": "fitness"}
    )
    assert campaign_resp.status_code == 200
    campaign = campaign_resp.json()
    assert campaign["status"] == "draft"
    assert campaign["template_id"] is None

    template_resp = client.post(
        "/templates",
        json={"campaign_id": campaign["id"], "variant": "A", "body": "Hi {{org_name}}!"},
    )
    assert template_resp.status_code == 200
    template = template_resp.json()
    assert template["campaign_id"] == campaign["id"]

    patch_resp = client.patch(f"/campaigns/{campaign['id']}", json={"template_id": template["id"]})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["template_id"] == template["id"]

    fetched = client.get(f"/campaigns/{campaign['id']}").json()
    assert fetched["template_id"] == template["id"]

    templates = client.get("/templates").json()
    assert any(t["id"] == template["id"] for t in templates)
