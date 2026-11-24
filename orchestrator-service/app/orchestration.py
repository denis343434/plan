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
_SEND_STOPPED_STATUSES = {"stopped"}
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


def run_campaign_flow(
    campaign_id: uuid.UUID, max_groups: int | None = None, parse_only: bool = False
) -> None:
    task = TASKS[campaign_id]

    data_client = DataServiceClient()
    parser_client = ParserServiceClient()
    messaging_client = MessagingServiceClient()
    try:
        campaign = data_client.get_campaign(campaign_id)
        data_client.update_campaign_status(campaign_id, "running")

        task.phase = OrchestrationPhase.parsing
        task.progress = None
        # parse_params (город/каналы у yandex_maps) уходят в filters парсера как есть — сам
        # Оркестратор их не интерпретирует.
        filters = dict(campaign.get("parse_params") or {})
        if max_groups is not None:
            filters["max_groups"] = max_groups
        parse_task = parser_client.start_parse(
            platform=campaign["platform"],
            keyword=campaign["keyword"],
            campaign_id=campaign_id,
            filters=filters or None,
            # По умолчанию True — для кампаний, созданных до появления этого поля в Data Service.
            use_account=campaign.get("use_account", True),
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

        if parse_only:
            # «Запустить только парсер»: даже если шаблон есть, рассылку не стартуем. Лиды
            # остаются status=new — их потом можно отправить отдельно кнопкой «только рассылка».
            task.phase = OrchestrationPhase.done
            task.note = "запущен только парсинг — рассылка не выполнялась"
            data_client.update_campaign_status(campaign_id, "completed")
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
            is_stopped=lambda s: s["status"] in _SEND_STOPPED_STATUSES,
            is_failed=lambda s: s["status"] in _SEND_FAILED_STATUSES,
            on_status=lambda s: setattr(
                task, "progress",
                {"sent": s.get("sent", 0), "failed": s.get("failed", 0), "skipped": s.get("skipped", 0)},
            ),
        )
        if outcome == "stopped":
            # Пользователь нажал «Стоп» в GUI — это не ошибка, кампания просто снимается с прогона.
            task.phase = OrchestrationPhase.done
            task.note = "рассылка остановлена пользователем"
            data_client.update_campaign_status(campaign_id, "paused")
            return
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
    # а не после падения там. Вариант F — повторное сообщение, первым сообщением он не
    # отправляется, поэтому кампания только с F считается кампанией без шаблона.
    if campaign.get("template_id"):
        return True
    templates = data_client.list_templates()
    return any(
        t.get("campaign_id") == str(campaign_id) and t.get("variant") != "F"
        for t in templates
    )


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
    is_stopped: Callable[[dict], bool] | None = None,
    on_status: Callable[[dict], None] | None = None,
    interval: float | None = None,
    stall_timeout: float | None = None,
) -> tuple[str, dict]:
    """Poll fetch_status() until is_done/is_failed, or until the status stops changing for
    stall_timeout secs. Returns (outcome, last_status).

    Раньше таймаут был жёстким потолком на всю фазу (POLL_TIMEOUT_SEC от старта поллинга) — на
    крупных кампаниях (сотни лидов) это ловило ещё живой, просто медленный прогресс (парсинг с
    паузами между кандидатами, рассылка с паузами между сообщениями) как "зависло", хотя
    status.progress исправно рос (см. запрос пользователя 2026-09-03, скриншот "фаза: таймаут ·
    ошибка: messaging: running" на кампании, которая ещё писала сообщения). Теперь таймаут не по
    общему времени фазы, а по времени БЕЗ изменений в status — зависшая (реально не отвечающая)
    задача по-прежнему ловится, а живая медленная — нет.
    """
    interval = settings.POLL_INTERVAL_SEC if interval is None else interval
    stall_timeout = settings.POLL_STALL_TIMEOUT_SEC if stall_timeout is None else stall_timeout

    last_status: dict | None = None
    stalled_since = time.monotonic()

    while True:
        status = fetch_status()
        if on_status is not None:
            on_status(status)
        if is_done(status):
            return "done", status
        if is_stopped is not None and is_stopped(status):
            return "stopped", status
        if is_failed(status):
            return "failed", status
        now = time.monotonic()
        if status != last_status:
            last_status = status
            stalled_since = now
        elif now - stalled_since >= stall_timeout:
            return "timeout", status
        time.sleep(interval)
