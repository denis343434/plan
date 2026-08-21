import logging
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from app.clients.data_service import DataServiceClient
from app.clients.messaging_service import MessagingServiceClient
from app.clients.parser_service import ParserServiceClient
from app.config import settings
from app.exceptions import DataServiceError, UpstreamServiceError

logger = logging.getLogger(__name__)

_PARSE_DONE_STATUSES = {"done"}
_PARSE_FAILED_STATUSES = {"failed"}

_SEND_DONE_STATUSES = {"done"}
_SEND_FAILED_STATUSES = {"failed", "waiting_for_account"}


class OrchestrationPhase(StrEnum):
    idle = "idle"
    parsing = "parsing"
    messaging = "messaging"
    done = "done"
    failed = "failed"
    timeout = "timeout"


@dataclass
class OrchestrationTask:
    campaign_id: uuid.UUID
    phase: OrchestrationPhase = OrchestrationPhase.idle
    error: str | None = None
    # Необязательное информационное сообщение (в отличие от error — не признак сбоя, просто
    # пояснение к финальному состоянию, например почему рассылка не запускалась).
    note: str | None = None
    # Живой прогресс текущей фазы (parsing: {"checked": N, "total": M} из Parser Service;
    # messaging: {"sent": N, "failed": M, "skipped": K} из Messaging Service) — обновляется
    # на каждом опросе _poll(), видно через GET /campaigns/{id} ещё до завершения фазы.
    progress: dict | None = None


TASKS: dict[uuid.UUID, OrchestrationTask] = {}


def create_task(campaign_id: uuid.UUID) -> OrchestrationTask:
    task = OrchestrationTask(campaign_id=campaign_id)
    TASKS[campaign_id] = task
    return task


def get_task(campaign_id: uuid.UUID) -> OrchestrationTask | None:
    return TASKS.get(campaign_id)


def run_campaign_flow(campaign_id: uuid.UUID, max_groups: int | None = None) -> None:
    task = TASKS[campaign_id]

    data_client = DataServiceClient()
    parser_client = ParserServiceClient()
    messaging_client = MessagingServiceClient()
    try:
        campaign = data_client.get_campaign(campaign_id)
        data_client.update_campaign_status(campaign_id, "running")

        task.phase = OrchestrationPhase.parsing
        task.progress = None
        parse_task = parser_client.start_parse(
            platform=campaign["platform"],
            keyword=campaign["keyword"],
            campaign_id=campaign_id,
            filters={"max_groups": max_groups} if max_groups is not None else None,
        )
        outcome, last_status = _poll(
            fetch_status=lambda: parser_client.get_parse_status(parse_task["task_id"]),
            is_done=lambda s: s["status"] in _PARSE_DONE_STATUSES,
            is_failed=lambda s: s["status"] in _PARSE_FAILED_STATUSES,
            on_status=lambda s: setattr(
                task, "progress",
                {"checked": s.get("progress_checked", 0), "total": s.get("progress_total", 0), "found": s.get("found", 0)},
            ),
        )
        if outcome != "done":
            _finish_with_problem(data_client, task, outcome, f"parser: {last_status.get('error') or last_status.get('status')}")
            return

        if not _has_template(data_client, campaign_id, campaign):
            # Без шаблона рассылать нечего — раньше это доходило до Messaging Service и
            # падало там ошибкой "no template configured for campaign", помечая кампанию как
            # сбойную. Теперь это ожидаемый исход: кампания завершается на одном парсинге.
            task.phase = OrchestrationPhase.done
            task.note = "шаблон сообщения не задан — рассылка пропущена, кампания завершена только парсингом"
            data_client.update_campaign_status(campaign_id, "completed")
            return

        task.phase = OrchestrationPhase.messaging
        task.progress = None
        messaging_client.start_send(campaign_id)
        outcome, last_status = _poll(
            fetch_status=lambda: messaging_client.get_send_status(campaign_id),
            is_done=lambda s: s["status"] in _SEND_DONE_STATUSES,
            is_failed=lambda s: s["status"] in _SEND_FAILED_STATUSES,
            on_status=lambda s: setattr(
                task, "progress",
                {"sent": s.get("sent", 0), "failed": s.get("failed", 0), "skipped": s.get("skipped", 0)},
            ),
        )
        if outcome != "done":
            _finish_with_problem(data_client, task, outcome, f"messaging: {last_status.get('error') or last_status.get('status')}")
            return

        task.phase = OrchestrationPhase.done
        data_client.update_campaign_status(campaign_id, "completed")

    except UpstreamServiceError as exc:
        _finish_with_problem(data_client, task, "failed", str(exc))
    finally:
        data_client.close()
        parser_client.close()
        messaging_client.close()


def _has_template(data_client: DataServiceClient, campaign_id: uuid.UUID, campaign: dict) -> bool:
    # Та же логика выбора шаблона, что и в Messaging Service (_resolve_templates) — держим
    # её здесь тоже, чтобы решение "запускать ли рассылку" принималось до старта фазы messaging,
    # а не после падения там.
    if campaign.get("template_id"):
        return True
    templates = data_client.list_templates()
    return any(t.get("campaign_id") == str(campaign_id) for t in templates)


def _finish_with_problem(
    data_client: DataServiceClient, task: OrchestrationTask, outcome: str, error: str
) -> None:
    task.phase = OrchestrationPhase.timeout if outcome == "timeout" else OrchestrationPhase.failed
    task.error = error
    try:
        data_client.update_campaign_status(task.campaign_id, "paused")
    except DataServiceError:
        logger.exception("failed to mark campaign %s as paused", task.campaign_id)


def _poll(
    fetch_status: Callable[[], dict],
    is_done: Callable[[dict], bool],
    is_failed: Callable[[dict], bool],
    on_status: Callable[[dict], None] | None = None,
    interval: float | None = None,
    timeout: float | None = None,
) -> tuple[str, dict]:
    """Poll fetch_status() until is_done/is_failed or timeout. Returns (outcome, last_status)."""
    interval = settings.POLL_INTERVAL_SEC if interval is None else interval
    timeout = settings.POLL_TIMEOUT_SEC if timeout is None else timeout
    deadline = time.monotonic() + timeout

    while True:
        status = fetch_status()
        if on_status is not None:
            on_status(status)
        if is_done(status):
            return "done", status
        if is_failed(status):
            return "failed", status
        if time.monotonic() >= deadline:
            return "timeout", status
        time.sleep(interval)
