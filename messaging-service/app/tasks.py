import logging
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from app import ratelimit, templating
from app.adapters.registry import get_adapter
from app.clients.data_service import DataServiceClient
from app.config import settings
from app.exceptions import DataServiceError, NoAccountAvailableError

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50
_FLOOD_COOLDOWN_MINUTES = 30


class SendTaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    waiting_for_account = "waiting_for_account"
    failed = "failed"


@dataclass
class SendTask:
    campaign_id: uuid.UUID
    status: SendTaskStatus = SendTaskStatus.queued
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None
    # Лиды, у которых отправка провалилась без признаков флуда (не аккаунт виноват, а сам
    # лид/сообщение) — остаются status=new в Data Service (ретраятся в следующем запуске
    # кампании), но в рамках ЭТОГО запуска повторно не берутся, иначе цикл ниже никогда
    # не увидит их вне статуса new и будет пытаться отправить один и тот же лид бесконечно.
    permanently_failed_lead_ids: set[str] = field(default_factory=set)


TASKS: dict[uuid.UUID, SendTask] = {}


def create_task(campaign_id: uuid.UUID) -> SendTask:
    task = SendTask(campaign_id=campaign_id)
    TASKS[campaign_id] = task
    return task


def get_task(campaign_id: uuid.UUID) -> SendTask | None:
    return TASKS.get(campaign_id)


def run_send_task(campaign_id: uuid.UUID) -> None:
    task = TASKS[campaign_id]
    task.status = SendTaskStatus.running

    client = DataServiceClient()
    try:
        templates = _resolve_templates(client, campaign_id)
        if not templates:
            task.status = SendTaskStatus.failed
            task.error = "no template configured for campaign"
            return

        while True:
            fetched = client.list_leads(status="new", campaign_id=campaign_id, limit=_BATCH_SIZE)
            leads = [lead for lead in fetched if lead["id"] not in task.permanently_failed_lead_ids]
            if not leads:
                # Либо реально закончились новые лиды, либо остались только те, что уже
                # провалились без флуда в этом запуске (см. permanently_failed_lead_ids) —
                # в обоих случаях продолжать цикл бессмысленно.
                break

            for lead in leads:
                if not _process_lead(client, task, lead, templates):
                    return  # waiting_for_account — задача останавливается, не ошибка

        task.status = SendTaskStatus.done

    except DataServiceError as exc:
        task.status = SendTaskStatus.failed
        task.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - не даём задаче зависнуть на "running" навсегда
        logger.exception("unhandled error in send task %s", campaign_id)
        task.status = SendTaskStatus.failed
        task.error = f"internal error: {exc}"
    finally:
        client.close()


def _resolve_templates(client: DataServiceClient, campaign_id: uuid.UUID) -> list[dict]:
    campaign = client.get_campaign(str(campaign_id))

    # GET /templates не фильтрует по campaign_id (нет такого параметра в Data Service) —
    # фильтруем на своей стороне, это и даёт настоящую A/B-ротацию, если под кампанию
    # заведено несколько вариантов шаблона с одинаковым campaign_id.
    templates = [t for t in client.list_templates() if t.get("campaign_id") == str(campaign_id)]
    if templates:
        return templates

    if campaign.get("template_id"):
        return [client.get_template(campaign["template_id"])]

    return []


def _process_lead(client: DataServiceClient, task: SendTask, lead: dict, templates: list[dict]) -> bool:
    try:
        account = client.next_available_account(
            platform=lead["platform"], purpose="messaging", task_ref=str(task.campaign_id)
        )
    except NoAccountAvailableError:
        task.status = SendTaskStatus.waiting_for_account
        return False

    cooled_down = False
    try:
        session = client.get_session(account["id"])
        adapter = get_adapter(lead["platform"], storage_state=session["storage_state"])

        template = templating.choose_template(templates)
        text = templating.render(template["body"], lead)

        result = adapter.send_message(lead, account, text)

        if result.success:
            client.patch_lead_status(lead["id"], "contacted")
            client.post_message(_message_payload(lead, account, template, text, "sent"))
            task.sent += 1
        else:
            client.post_message(_message_payload(lead, account, template, text, "failed"))
            task.failed += 1
            if result.flood_detected:
                client.cooldown_account(
                    account["id"], minutes=_FLOOD_COOLDOWN_MINUTES, reason=result.error or "flood_detected"
                )
                cooled_down = True
            else:
                # Не проблема аккаунта — лид остаётся status=new (можно ретраить в следующем
                # запуске кампании), но в рамках текущего run_send_task больше не берём его,
                # иначе внешний while-цикл будет ретраить его бесконечно.
                task.permanently_failed_lead_ids.add(lead["id"])
    finally:
        if not cooled_down:
            try:
                client.release_account(account["id"])
            except DataServiceError:
                logger.exception("failed to release account %s", account["id"])

    ratelimit.delay(settings.MIN_DELAY_SEC, settings.MAX_DELAY_SEC)
    return True


def _message_payload(lead: dict, account: dict, template: dict, text: str, delivery_status: str) -> dict:
    return {
        "lead_id": lead["id"],
        "account_id": account["id"],
        "template_variant": template.get("variant"),
        "text_sent": text,
        "delivery_status": delivery_status,
    }
