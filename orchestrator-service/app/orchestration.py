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


TASKS: dict[uuid.UUID, OrchestrationTask] = {}


def create_task(campaign_id: uuid.UUID) -> OrchestrationTask:
    task = OrchestrationTask(campaign_id=campaign_id)
    TASKS[campaign_id] = task
    return task


def get_task(campaign_id: uuid.UUID) -> OrchestrationTask | None:
    return TASKS.get(campaign_id)


def run_campaign_flow(campaign_id: uuid.UUID) -> None:
    task = TASKS[campaign_id]

    data_client = DataServiceClient()
    parser_client = ParserServiceClient()
    messaging_client = MessagingServiceClient()
    try:
        campaign = data_client.get_campaign(campaign_id)
        data_client.update_campaign_status(campaign_id, "running")

        task.phase = OrchestrationPhase.parsing
        parse_task = parser_client.start_parse(
            platform=campaign["platform"], keyword=campaign["keyword"], campaign_id=campaign_id
        )
        outcome, last_status = _poll(
            fetch_status=lambda: parser_client.get_parse_status(parse_task["task_id"]),
            is_done=lambda s: s["status"] in _PARSE_DONE_STATUSES,
            is_failed=lambda s: s["status"] in _PARSE_FAILED_STATUSES,
        )
        if outcome != "done":
            _finish_with_problem(data_client, task, outcome, f"parser: {last_status.get('error') or last_status.get('status')}")
            return

        task.phase = OrchestrationPhase.messaging
        messaging_client.start_send(campaign_id)
        outcome, last_status = _poll(
            fetch_status=lambda: messaging_client.get_send_status(campaign_id),
            is_done=lambda s: s["status"] in _SEND_DONE_STATUSES,
            is_failed=lambda s: s["status"] in _SEND_FAILED_STATUSES,
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
    interval: float | None = None,
    timeout: float | None = None,
) -> tuple[str, dict]:
    """Poll fetch_status() until is_done/is_failed or timeout. Returns (outcome, last_status)."""
    interval = settings.POLL_INTERVAL_SEC if interval is None else interval
    timeout = settings.POLL_TIMEOUT_SEC if timeout is None else timeout
    deadline = time.monotonic() + timeout

    while True:
        status = fetch_status()
        if is_done(status):
            return "done", status
        if is_failed(status):
            return "failed", status
        if time.monotonic() >= deadline:
            return "timeout", status
        time.sleep(interval)
