import random
import re

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(body: str, lead: dict) -> str:
    context = {
        "org_name": lead.get("title") or lead.get("group_url", ""),
        "group_url": lead.get("group_url", ""),
        "admin_contact": lead.get("admin_contact") or "",
    }
    return _PLACEHOLDER_RE.sub(lambda m: str(context.get(m.group(1), "")), body)


def choose_template(templates: list[dict]) -> dict:
    return random.choice(templates)
