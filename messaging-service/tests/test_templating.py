from app import templating


def test_render_substitutes_known_placeholders():
    lead = {"title": "Fitness Club", "group_url": "https://vk.com/fitness_club", "admin_contact": "ivan"}
    body = "Hi {{org_name}}, see {{group_url}}, contact {{admin_contact}}"

    text = templating.render(body, lead)

    assert text == "Hi Fitness Club, see https://vk.com/fitness_club, contact ivan"


def test_render_falls_back_to_group_url_when_no_title():
    lead = {"title": None, "group_url": "https://vk.com/no_title_group", "admin_contact": None}
    text = templating.render("{{org_name}} / {{admin_contact}}", lead)

    assert text == "https://vk.com/no_title_group / "


def test_render_leaves_unknown_placeholder_blank():
    text = templating.render("Hello {{unknown_key}}!", {"title": "X", "group_url": "u"})
    assert text == "Hello !"


def test_choose_template_alternates_over_many_draws():
    templates = [{"variant": "A", "body": "a"}, {"variant": "B", "body": "b"}]

    seen = {templating.choose_template(templates)["variant"] for _ in range(200)}

    assert seen == {"A", "B"}


def test_choose_template_single_option_is_stable():
    templates = [{"variant": "A", "body": "a"}]
    assert templating.choose_template(templates) == templates[0]
