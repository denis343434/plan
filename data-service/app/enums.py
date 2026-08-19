from enum import StrEnum


class Platform(StrEnum):
    vk = "vk"
    tg = "tg"
    instagram = "instagram"


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
