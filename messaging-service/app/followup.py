import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from app import ratelimit, templating
from app.adapter_pool import SendAdapterPool
from app.adapters.registry import get_adapter
from app.clients.data_service import DataServiceClient
from app.config import settings
from app.exceptions import DataServiceError, NoAccountAvailableError
from app.tasks import campaign_skips_site_check

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50
_FLOOD_COOLDOWN_MINUTES = 30


class FollowupTaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    waiting_for_account = "waiting_for_account"
    failed = "failed"


@dataclass
class FollowupTask:
    campaign_id: uuid.UUID
    status: FollowupTaskStatus = FollowupTaskStatus.queued
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None


TASKS: dict[uuid.UUID, FollowupTask] = {}


def create_task(campaign_id: uuid.UUID) -> FollowupTask:
    task = FollowupTask(campaign_id=campaign_id)
    TASKS[campaign_id] = task
    return task


def get_task(campaign_id: uuid.UUID) -> FollowupTask | None:
    return TASKS.get(campaign_id)


def run_followup_task(campaign_id: uuid.UUID) -> None:
    """Лидам со status=contacted (первое сообщение уже отправлено), которые не ответили
    settings.FOLLOWUP_DELAY_DAYS дней, шлёт ровно одно повторное сообщение по отдельному
    шаблону кампании (variant=templating.FOLLOWUP_VARIANT).

    Статус лида не меняется — той же связкой contacted/replied по-прежнему заведует обычная
    проверка входящих (app/inbox.py): если ответ придёт уже после повтора, лид всё так же
    станет replied на следующей проверке. "Уже ответил" здесь поэтому не проверяется отдельно —
    ответивший лид переходит в status=replied и просто не попадает в выборку status=contacted.
    """
    task = TASKS[campaign_id]
    task.status = FollowupTaskStatus.running

    client = DataServiceClient()
    # Тот же пул браузеров на аккаунт, что и в tasks.py::run_send_task.
    adapters = SendAdapterPool(client, lambda platform, storage_state: get_adapter(platform, storage_state=storage_state))
    try:
        template = _resolve_followup_template(client, campaign_id)
        if template is None:
            task.status = FollowupTaskStatus.failed
            task.error = "no follow-up template configured for campaign"
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.FOLLOWUP_DELAY_DAYS)
        skip_site_check = campaign_skips_site_check(client, campaign_id)

        offset = 0
        while True:
            leads = client.list_leads(
                status="contacted", campaign_id=campaign_id, limit=_BATCH_SIZE, offset=offset
            )
            if not leads:
                break
            offset += len(leads)

            for lead in leads:
                lead["skip_site_check"] = skip_site_check
                if not _process_lead(client, adapters, task, lead, template, cutoff):
                    return  # waiting_for_account — задача останавливается, не ошибка

        task.status = FollowupTaskStatus.done

    except DataServiceError as exc:
        task.status = FollowupTaskStatus.failed
        task.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - не даём задаче зависнуть на "running" навсегда
        logger.exception("unhandled error in followup task %s", campaign_id)
        task.status = FollowupTaskStatus.failed
        task.error = f"internal error: {exc}"
    finally:
        adapters.close()
        client.close()


def _resolve_followup_template(client: DataServiceClient, campaign_id: uuid.UUID) -> dict | None:
    templates = [
        t for t in client.list_templates()
        if t.get("campaign_id") == str(campaign_id) and t.get("variant") == templating.FOLLOWUP_VARIANT
    ]
    return templates[0] if templates else None


def _process_lead(
    client: DataServiceClient,
    adapters: SendAdapterPool,
    task: FollowupTask,
    lead: dict,
    template: dict,
    cutoff: datetime,
) -> bool:
    cycle_started = time.monotonic()
    messages = client.list_messages(lead_id=lead["id"])

    if any(m["template_variant"] == templating.FOLLOWUP_VARIANT for m in messages):
        task.skipped += 1
        return True  # повтор этому лиду уже отправляли раньше

    sent_at_values = [_parse_sent_at(m["sent_at"]) for m in messages if m["delivery_status"] == "sent"]
    if not sent_at_values or max(sent_at_values) > cutoff:
        task.skipped += 1
        return True  # либо изначальное сообщение так и не ушло, либо ещё не прошло FOLLOWUP_DELAY_DAYS

    try:
        account = client.next_available_account(
            platform=lead["platform"], purpose="messaging", task_ref=f"followup:{task.campaign_id}"
        )
    except NoAccountAvailableError:
        task.status = FollowupTaskStatus.waiting_for_account
        return False

    cooled_down = False
    try:
        try:
            adapter = adapters.get(account, lead["platform"])
        except DataServiceError as exc:
            # Та же логика, что и в tasks.py::_process_lead — проблема конкретно этого аккаунта
            # не должна ронять всю задачу, лид просто останется без повтора в этом запуске.
            logger.warning(
                "account %s unusable for lead %s (%s), skipping this attempt", account["id"], lead["id"], exc
            )
            task.failed += 1
            return True

        text = templating.render(template["body"], lead)
        result = adapter.send_message(lead, account, text)

        if result.success:
            client.post_message(_message_payload(lead, account, template, text, "sent"))
            task.sent += 1
        else:
            delivery_status = "pending" if result.network_error else "failed"
            client.post_message(_message_payload(lead, account, template, text, delivery_status, result.error))
            task.failed += 1
            if result.flood_detected or result.session_expired:
                reason = result.error or ("session_expired" if result.session_expired else "flood_detected")
                client.cooldown_account(account["id"], minutes=_FLOOD_COOLDOWN_MINUTES, reason=reason)
                cooled_down = True
                adapters.discard(account, lead["platform"])
    finally:
        if not cooled_down:
            try:
                client.release_account(account["id"])
            except DataServiceError:
                logger.exception("failed to release account %s", account["id"])

    ratelimit.delay_for_platform(
        lead["platform"], elapsed_sec=time.monotonic() - cycle_started
    )
    return True


def _message_payload(
    lead: dict, account: dict, template: dict, text: str, delivery_status: str, error_reason: str | None = None
) -> dict:
    return {
        "lead_id": lead["id"],
        "account_id": account["id"],
        "template_variant": template.get("variant"),
        "text_sent": text,
        "delivery_status": delivery_status,
        "error_reason": error_reason,
    }


def _parse_sent_at(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
