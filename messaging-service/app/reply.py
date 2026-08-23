import logging

from app import ratelimit
from app.adapters.registry import get_adapter
from app.clients.data_service import DataServiceClient
from app.config import settings
from app.exceptions import DataServiceError

logger = logging.getLogger(__name__)

_REPLY_LOCK_TTL_SECONDS = 300
_FLOOD_COOLDOWN_MINUTES = 30


def send_reply(lead_id: str, account_id: str, text: str, image: dict | None = None) -> dict:
    client = DataServiceClient()
    try:
        lead = client.get_lead(lead_id)
        account = client.lock_account(
            account_id, lock_ttl_seconds=_REPLY_LOCK_TTL_SECONDS, task_ref=f"reply:{lead_id}"
        )

        cooled_down = False
        try:
            session = client.get_session(account_id)
            adapter = get_adapter(lead["platform"], storage_state=session["storage_state"])
            result = adapter.send_message(lead, account, text, image=image)

            if result.success:
                message = client.post_message(_message_payload(lead_id, account_id, text, "sent"))
            else:
                message = client.post_message(
                    _message_payload(lead_id, account_id, text, "failed", result.error)
                )
                if result.flood_detected:
                    client.cooldown_account(
                        account_id, minutes=_FLOOD_COOLDOWN_MINUTES, reason=result.error or "flood_detected"
                    )
                    cooled_down = True
        finally:
            if not cooled_down:
                try:
                    client.release_account(account_id)
                except DataServiceError:
                    logger.exception("failed to release account %s", account_id)

        ratelimit.delay(settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC)
        return message
    finally:
        client.close()


def _message_payload(lead_id: str, account_id: str, text: str, delivery_status: str, error_reason: str | None = None) -> dict:
    return {
        "lead_id": lead_id,
        "account_id": account_id,
        "template_variant": None,
        "text_sent": text,
        "delivery_status": delivery_status,
        "error_reason": error_reason,
    }
