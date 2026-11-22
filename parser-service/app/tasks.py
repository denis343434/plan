import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

from app.adapters import contacts
from app.adapters.base import (
    AntiBotChallengeError,
    BrowserCrashedError,
    CaptchaDetectedError,
    ParseFilters,
)
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
    # Аккаунты Instagram/Facebook, занятые под поиск организаций Google Карт (см. _lock_social_accounts).
    social_account_ids: list[str] = []
    try:
        parse_filters = ParseFilters(has_site=filters.has_site, max_groups=filters.max_groups, city=filters.city)
        if filters.channels:
            parse_filters.channels = tuple(c for c in filters.channels if c in contacts.CHANNELS)
        parse_filters.whatsapp_by_phone = filters.whatsapp_by_phone
        parse_filters.search_socials = filters.search_socials or filters.tg_by_phone

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
            if platform == "google_maps" and parse_filters.search_socials:
                social_sessions = _lock_social_accounts(client, parse_filters.channels, task_id, social_account_ids)
                adapter = get_adapter(platform, social_sessions=social_sessions)
            else:
                adapter = get_adapter(platform)

        # Dedup-лист (см. ParseFilters.known_external_ids) нужен любому платформенному адаптеру,
        # требующему браузер (vk, instagram), независимо от того, используется ли аккаунт —
        # список уже известных лидов приходит из Data Service, а не из сессии аккаунта. Считаем
        # его уже после того, как решили, что парсинг вообще состоится (см. return выше).
        if platform in contacts.MAP_SOURCES:
            # Лиды Яндекс/Google Карт пишутся с платформой канала связи (vk/tg/whatsapp/facebook) — dedup
            # собираем по всем этим платформам сразу, с ключом "platform:external_id" (см.
            # contacts.pick_lead): организацию, которой уже писали хоть через какой-то канал из
            # приоритетного, второй раз не берём и в лимит max_groups не считаем.
            try:
                parse_filters.known_external_ids = frozenset(
                    f"{lead['platform']}:{lead['external_id']}"
                    for channel in contacts.CHANNELS
                    for lead in client.list_leads(platform=channel)
                )
            except DataServiceError:
                logger.exception("failed to fetch known leads for dedup-aware parsing, proceeding without it")
        elif requires_browser:
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
        except AntiBotChallengeError as exc:
            # VK-проверка "Проверяем, что вы не робот" привязана к IP/браузеру, а не к аккаунту —
            # держать из-за неё рабочий аккаунт в 30-минутном кулдауне неправильно (каждая
            # попытка парсинга "закрывала" бы его на полчаса). Просто фиксируем ошибку прогона.
            raw_leads = exc.partial_leads
            task.error = str(exc)
        except CaptchaDetectedError as exc:
            raw_leads = exc.partial_leads
            task.error = str(exc)
            if account_id is not None:
                client.cooldown_account(account_id, minutes=30, reason="captcha_detected")
                # Аккаунт уже переведён в cooldown — release() ниже в finally не должен
                # затирать это обратно на active (см. account.release() в Data Service,
                # которая теперь тоже это учитывает; здесь — defense-in-depth).
                cooled_down = True
        except BrowserCrashedError as exc:
            # Браузер/драйвер Playwright умер посреди проверки кандидатов — то, что уже успели
            # найти, сохраняем (раньше терялось вместе с зависшей задачей), задачу помечаем failed.
            # Аккаунт тут ни при чём — обычный release в finally, без cooldown.
            logger.warning("parse task %s: %s (saving %d partial leads)", task_id, exc, len(exc.partial_leads))
            raw_leads = exc.partial_leads
            task.error = str(exc)

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
        for social_id in social_account_ids:
            try:
                client.release_account(social_id)
            except DataServiceError:
                logger.exception("failed to release social lookup account %s", social_id)
        if account_id is not None and not cooled_down:
            try:
                client.release_account(account_id)
            except DataServiceError:
                logger.exception("failed to release/cooldown account %s", account_id)
        client.close()


# Поиск по соцсетям может идти часами (каждая карточка без ссылки — несколько запросов), lock
# держим с запасом; release в finally run_parse_task всё равно отпускает его сразу по окончании.
_SOCIAL_LOCK_TTL_SECONDS = 6 * 3600


def _lock_social_accounts(
    client: DataServiceClient, channels: tuple[str, ...], task_id: uuid.UUID, locked_ids: list[str]
) -> dict[str, dict]:
    """Сессии аккаунтов для поиска организаций в Instagram/Facebook (только тех, что выбраны
    каналами). Нет аккаунта — эта соцсеть просто не ищется, парсинг идёт дальше."""
    sessions: dict[str, dict] = {}
    for channel in ("instagram", "facebook"):
        if channel not in channels:
            continue
        try:
            account = client.next_available_account(
                platform=channel, purpose="parsing", lock_ttl_seconds=_SOCIAL_LOCK_TTL_SECONDS, task_ref=str(task_id)
            )
        except NoAccountAvailableError:
            logger.warning("no %s account for social lookup — searching without it", channel)
            continue
        locked_ids.append(account["id"])
        storage_state = client.get_session(account["id"]).get("storage_state")
        if storage_state:
            sessions[channel] = storage_state
    return sessions


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
                "platform": lead.platform or platform,
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
