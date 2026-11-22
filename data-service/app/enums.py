from enum import StrEnum


class Platform(StrEnum):
    vk = "vk"
    tg = "tg"
    instagram = "instagram"
    whatsapp = "whatsapp"
    # Пока только аккаунт с сохранённой сессией — ни парсера, ни отправки под него ещё нет.
    facebook = "facebook"
    # Только у кампаний, не у лидов/аккаунтов: источник — Яндекс Карты, а найденные лиды
    # пишутся с платформой канала связи организации (vk/tg/whatsapp), см.
    # parser-service/app/adapters/yandex_maps.py.
    yandex_maps = "yandex_maps"
    # То же для Google Карт (parser-service/app/adapters/google_maps.py).
    google_maps = "google_maps"


class LeadStatus(StrEnum):
    new = "new"
    queued = "queued"
    contacted = "contacted"
    replied = "replied"
    rejected = "rejected"


class CampaignStatus(StrEnum):
    draft = "draft"
    running = "running"
    paused = "paused"
    completed = "completed"


class AccountPurpose(StrEnum):
    parsing = "parsing"
    messaging = "messaging"
    both = "both"


class AccountStatus(StrEnum):
    active = "active"
    flood = "flood"
    banned = "banned"
    cooldown = "cooldown"
    locked = "locked"


class DeliveryStatus(StrEnum):
    sent = "sent"
    failed = "failed"
    pending = "pending"


class ReplyStatus(StrEnum):
    none = "none"
    replied = "replied"
    no_reply = "no_reply"
