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


def test_saving_same_variant_twice_updates_instead_of_duplicating(client: TestClient) -> None:
    campaign = client.post(
        "/campaigns", json={"name": "Cafe", "platform": "vk", "keyword": "cafe"}
    ).json()

    first = client.post(
        "/templates", json={"campaign_id": campaign["id"], "variant": "A", "body": "Здравствуйте!"}
    ).json()
    second = client.post(
        "/templates", json={"campaign_id": campaign["id"], "variant": "A", "body": "Добрый день!"}
    ).json()

    # Тот же id, обновлённый текст — не вторая строка (см. запрос пользователя 2026-08-24:
    # каждый клик "Сохранить шаблон" по одному и тому же варианту плодил дубликаты).
    assert second["id"] == first["id"]
    assert second["body"] == "Добрый день!"

    own = [t for t in client.get("/templates").json() if t["campaign_id"] == campaign["id"]]
    assert len(own) == 1


def test_delete_template_removes_it(client: TestClient) -> None:
    campaign = client.post(
        "/campaigns", json={"name": "Gym", "platform": "vk", "keyword": "gym"}
    ).json()
    template = client.post(
        "/templates", json={"campaign_id": campaign["id"], "variant": "A", "body": "Hi!"}
    ).json()

    delete_resp = client.delete(f"/templates/{template['id']}")
    assert delete_resp.status_code == 204

    assert client.get(f"/templates/{template['id']}").status_code == 404
    own = [t for t in client.get("/templates").json() if t["campaign_id"] == campaign["id"]]
    assert own == []
