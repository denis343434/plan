import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

from app.adapters.base import CaptchaDetectedError, ParseFilters
from app.adapters.registry import get_adapter
from app.clients.data_service import DataServiceClient
from app.exceptions import DataServiceError, NoAccountAvailableError
from app.schemas.parse import ParseFiltersSchema

logger = logging.getLogger(__name__)

_BATCH_SIZE = 200


class TaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


@dataclass
class ParseTask:
    task_id: uuid.UUID
    platform: str
    keyword: str
    status: TaskStatus = TaskStatus.queued
    found: int = 0
    inserted: int = 0
    skipped: int = 0
    error: str | None = None
    # Живой прогресс проверки кандидатов на фильтр "есть сайт" (см. adapters/vk.py) —
    # checked/progress_total растут по ходу поиска, до того как found вообще станет известен.
    progress_checked: int = 0
    progress_total: int = 0


TASKS: dict[uuid.UUID, ParseTask] = {}


def create_task(platform: str, keyword: str) -> ParseTask:
    task = ParseTask(task_id=uuid.uuid4(), platform=platform, keyword=keyword)
    TASKS[task.task_id] = task
    return task


def get_task(task_id: uuid.UUID) -> ParseTask | None:
    return TASKS.get(task_id)


def run_parse_task(
    task_id: uuid.UUID,
    platform: str,
    keyword: str,
    filters: ParseFiltersSchema,
    campaign_id: uuid.UUID | None,
    use_account: bool = True,
) -> None:
    task = TASKS[task_id]
    task.status = TaskStatus.running

    client = DataServiceClient()
    account_id: str | None = None
    cooled_down = False
    try:
        parse_filters = ParseFilters(has_site=filters.has_site, max_groups=filters.max_groups)

        # Instagram блокирует анонимный доступ значительно агрессивнее VK (см. plan-instagram.md) —
        # у адаптера попросту нет рабочего анонимного пути (поиск/просмотр профиля упираются в
        # login wall почти сразу), поэтому эту комбинацию отклоняем сразу и явно, а не запускаем
        # браузер на заведомо провальный прогон, который просто зависнет на таймаутах.
        if platform == "instagram" and not use_account:
            task.status = TaskStatus.failed
            task.error = "Instagram не поддерживает парсинг без аккаунта — включите режим «в аккаунте»"
            return

        requires_browser = platform in ("vk", "instagram")
        if requires_browser and use_account:
            try:
                account = client.next_available_account(
                    platform=platform, purpose="parsing", task_ref=str(task_id)
                )
            except NoAccountAvailableError:
                # Аккаунта нет — выходим сразу, ДО запроса dedup-листа ниже: он не бесплатный
                # (ходит в Data Service) и всё равно бесполезен, раз парсинг в этом прогоне не
                # состоится вообще.
                task.status = TaskStatus.failed
                task.error = "no account available"
                return

            account_id = account["id"]
            session = client.get_session(account_id)
            adapter = get_adapter(platform, storage_state=session["storage_state"])
        else:
            # use_account=False (или платформа без реального адаптера) — не трогаем пул
            # аккаунтов вообще: анонимный режим открывает браузер без сохранённой сессии (см.
            # adapters/vk.py — при пустом storage_state контекст Playwright создаётся без
            # cookies, ровно как для гостя). Ни lock/release, ни cooldown тут не при чём —
            # аккаунт в этом прогоне не занимается.
            adapter = get_adapter(platform)

        # Dedup-лист (см. ParseFilters.known_external_ids) нужен любому платформенному адаптеру,
        # требующему браузер (vk, instagram), независимо от того, используется ли аккаунт —
        # список уже известных лидов приходит из Data Service, а не из сессии аккаунта. Считаем
        # его уже после того, как решили, что парсинг вообще состоится (см. return выше).
        if requires_browser:
            try:
                parse_filters.known_external_ids = frozenset(
                    lead["external_id"] for lead in client.list_leads(platform=platform)
                )
            except DataServiceError:
                logger.exception("failed to fetch known leads for dedup-aware parsing, proceeding without it")

        def on_progress(checked: int, total: int, found: int) -> None:
            task.progress_checked = checked
            task.progress_total = total
            task.found = found

        try:
            raw_leads = adapter.search_communities(keyword, parse_filters, on_progress=on_progress)
        except CaptchaDetectedError as exc:
            raw_leads = exc.partial_leads
            task.error = str(exc)
            if account_id is not None:
                client.cooldown_account(account_id, minutes=30, reason="captcha_detected")
                # Аккаунт уже переведён в cooldown — release() ниже в finally не должен
                # затирать это обратно на active (см. account.release() в Data Service,
                # которая теперь тоже это учитывает; здесь — defense-in-depth).
                cooled_down = True

        task.found = len(raw_leads)
        _insert_leads(client, platform, campaign_id, raw_leads, task)

        task.status = TaskStatus.failed if task.error else TaskStatus.done

    except DataServiceError as exc:
        task.status = TaskStatus.failed
        task.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - не даём задаче зависнуть на "running" навсегда
        logger.exception("unhandled error in parse task %s", task_id)
        task.status = TaskStatus.failed
        task.error = f"internal error: {exc}"
    finally:
        if account_id is not None and not cooled_down:
            try:
                client.release_account(account_id)
            except DataServiceError:
                logger.exception("failed to release/cooldown account %s", account_id)
        client.close()


def _insert_leads(
    client: DataServiceClient,
    platform: str,
    campaign_id: uuid.UUID | None,
    raw_leads: list,
    task: ParseTask,
) -> None:
    for start in range(0, len(raw_leads), _BATCH_SIZE):
        batch = raw_leads[start : start + _BATCH_SIZE]
        payload = [
            {
                "platform": platform,
                "external_id": lead.external_id,
                "group_url": lead.group_url,
                "admin_contact": lead.admin_contact,
                "title": lead.title,
                "campaign_id": str(campaign_id) if campaign_id else None,
            }
            for lead in batch
        ]
        result = client.bulk_insert_leads(payload)
        task.inserted += result["inserted"]
        task.skipped += result["skipped"]
