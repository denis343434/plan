import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from app import ratelimit, templating
from app.adapter_pool import SendAdapterPool
from app.adapters.base import SendResult
from app.adapters.registry import get_adapter
from app.clients.data_service import DataServiceClient
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


def run_send_task(campaign_id: uuid.UUID, retry_failed: bool = False) -> None:
    task = TASKS[campaign_id]
    task.status = SendTaskStatus.running

    client = DataServiceClient()
    # Браузеры аккаунтов живут на весь запуск (см. SendAdapterPool) — закрываются в finally ниже.
    adapters = SendAdapterPool(client, lambda platform, storage_state: get_adapter(platform, storage_state=storage_state))
    try:
        templates = _resolve_templates(client, campaign_id)
        if not templates:
            task.status = SendTaskStatus.failed
            task.error = "no template configured for campaign"
            return
        skip_site_check = campaign_skips_site_check(client, campaign_id)

        # Провалившийся (не по флуду) лид остаётся status=new в Data Service (см. _process_lead) —
        # без этой проверки он подхватывался бы каждым обычным запуском кампании точно так же,
        # как никогда не тронутый лид, и бот писал бы туда снова и снова при каждом старте.
        # retry_failed=True (кнопка "Повторить с ошибками" в десктоп-клиенте) явно включает такие
        # лиды обратно в выборку.
        previously_failed_lead_ids: set[str] = set()
        if not retry_failed:
            previously_failed_lead_ids = {
                m["lead_id"] for m in client.list_messages() if m["delivery_status"] == "failed"
            }

        # Пропущенные лиды (провалившиеся раньше или в этом запуске) остаются status=new и всегда
        # возвращаются Data Service в начале выборки (order_by found_at) — offset сдвигаем ровно
        # на них. Без этого, если целый батч состоял из ранее провалившихся лидов, цикл решал,
        # что новых лидов нет, и остальные (за пределами первых _BATCH_SIZE) не писались никогда.
        offset = 0
        while True:
            fetched = client.list_leads(
                status="new", campaign_id=campaign_id, limit=_BATCH_SIZE, offset=offset
            )
            leads = [
                lead for lead in fetched
                if lead["id"] not in task.permanently_failed_lead_ids
                and lead["id"] not in previously_failed_lead_ids
            ]
            if not leads and len(fetched) < _BATCH_SIZE:
                # Дошли до конца выборки: остались только пропускаемые лиды (или вообще ничего).
                break

            for lead in leads:
                lead["skip_site_check"] = skip_site_check
                if not _process_lead(client, adapters, task, lead, templates):
                    return  # waiting_for_account — задача останавливается, не ошибка

            offset += sum(
                1 for lead in fetched
                if lead["id"] in task.permanently_failed_lead_ids
                or lead["id"] in previously_failed_lead_ids
            )

        task.status = SendTaskStatus.done

    except DataServiceError as exc:
        task.status = SendTaskStatus.failed
        task.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - не даём задаче зависнуть на "running" навсегда
        logger.exception("unhandled error in send task %s", campaign_id)
        task.status = SendTaskStatus.failed
        task.error = f"internal error: {exc}"
    finally:
        adapters.close()
        client.close()


def campaign_skips_site_check(client: DataServiceClient, campaign_id: uuid.UUID) -> bool:
    """У кампаний Яндекс/Google Карт фильтр "без сайта" уже применён парсером по карточке организации
    (или выключен пользователем намеренно) — проверка сайта на странице VK-группы перед отправкой
    (VkSendAdapter._has_external_site, страховка от ошибок VK-парсера) здесь только мешала бы."""
    return client.get_campaign(str(campaign_id)).get("platform") in ("yandex_maps", "google_maps")


def _resolve_templates(client: DataServiceClient, campaign_id: uuid.UUID) -> list[dict]:
    campaign = client.get_campaign(str(campaign_id))

    # GET /templates не фильтрует по campaign_id (нет такого параметра в Data Service) —
    # фильтруем на своей стороне, это и даёт настоящую A/B-ротацию, если под кампанию
    # заведено несколько вариантов шаблона с одинаковым campaign_id. Шаблон повторного
    # сообщения (variant=templating.FOLLOWUP_VARIANT, см. app/followup.py) сюда не входит —
    # иначе choose_template мог бы разослать его как обычное первое сообщение.
    templates = [
        t for t in client.list_templates()
        if t.get("campaign_id") == str(campaign_id) and t.get("variant") != templating.FOLLOWUP_VARIANT
    ]
    if templates:
        return templates

    if campaign.get("template_id"):
        return [client.get_template(campaign["template_id"])]

    return []


def _process_lead(
    client: DataServiceClient, adapters: SendAdapterPool, task: SendTask, lead: dict, templates: list[dict]
) -> bool:
    # Аккаунт может быть временно занят (параллельный парсинг/другой прогон) — несколько коротких
    # повторов вместо мгновенного "waiting_for_account", из-за которого рассылка вставала на
    # первом же лиде (см. кампанию "Строительство домов": 16 попыток и стоп).
    account = None
    for attempt in range(3):
        try:
            account = client.next_available_account(
                platform=lead["platform"], purpose="messaging", task_ref=str(task.campaign_id)
            )
            break
        except NoAccountAvailableError as exc:
            if attempt == 2:
                task.status = SendTaskStatus.waiting_for_account
                logger.warning("no account available for lead %s: %s", lead["id"], exc)
                return False
            time.sleep(5)

    cooled_down = False
    adapter = None
    try:
        try:
            adapter = adapters.get(account, lead["platform"])
        except DataServiceError as exc:
            # Проблема конкретно с ЭТИМ аккаунтом (например нет сохранённой сессии) не должна
            # ронять всю задачу — иначе при нескольких аккаунтах в пуле один "битый" тормозит
            # рассылку для остальных, рабочих. Лид остаётся status=new и ретраится в следующем
            # запуске кампании — как и любая другая непроблема-лида ошибка отправки ниже.
            logger.warning("account %s unusable for lead %s (%s), skipping this attempt", account["id"], lead["id"], exc)
            task.failed += 1
            task.permanently_failed_lead_ids.add(lead["id"])
            return True

        template = templating.choose_template(templates)
        text = templating.render(template["body"], lead)

        try:
            result = adapter.send_message(lead, account, text)
        except Exception as exc:  # noqa: BLE001 - сбой адаптера не должен ронять всю рассылку
            # Раньше TargetClosedError из Playwright (закрыли окно браузера/упал процесс)
            # вылетал наверх и убивал всю задачу: оставшиеся лиды даже не пробовали писать.
            # Теперь считаем это транзиентным сбоем, выбрасываем сломанный браузер аккаунта
            # и идём дальше — лид подхватится следующим запуском.
            logger.exception("adapter crashed while sending to lead %s, will retry next run", lead["id"])
            adapters.discard(account, lead["platform"])
            result = SendResult(success=False, error=f"internal error: {exc}", network_error=True)

        if result.success:
            client.patch_lead_status(lead["id"], "contacted")
            client.post_message(_message_payload(lead, account, template, text, "sent"))
            task.sent += 1
        elif result.blocked:
            # VK-проверка "Проверяем, что вы не робот": останавливаем прогон с понятной ошибкой,
            # но без кулдауна аккаунта и без пометки лида (см. SendResult.blocked).
            task.status = SendTaskStatus.failed
            task.error = result.error
            return False
        elif result.unreachable or result.has_site:
            # Этому сообществу писать нельзя/не нужно (уже есть свой сайт или отключён приём
            # сообщений) — это НЕ ошибка отправки, а пропуск. Сообщение в БД не пишем (иначе
            # попытки-пропуски жрали бы часовой лимит аккаунта), лид сразу переводим в
            # rejected, чтобы он не висел вечно в "новых" и не пробовался каждый прогон.
            logger.info("lead %s unreachable, marking rejected: %s", lead["id"], result.error)
            client.patch_lead_status(lead["id"], "rejected")
            task.permanently_failed_lead_ids.add(lead["id"])
            task.skipped += 1
        else:
            # network_error — страница не загрузилась (плохой интернет), не проблема лида: пишем
            # "pending", а не "failed" — previously_failed_lead_ids выше фильтрует только по
            # delivery_status=="failed", так что "pending"-лид (status=new в Data Service)
            # подхватится обычным следующим запуском кампании сам, без ручного "Повторить с
            # ошибками" (тот же принцип, что и в parser-service для _has_external_site, см.
            # запрос пользователя).
            delivery_status = "pending" if result.network_error else "failed"
            client.post_message(_message_payload(lead, account, template, text, delivery_status, result.error))
            task.failed += 1
            if result.flood_detected or result.session_expired:
                reason = result.error or ("session_expired" if result.session_expired else "flood_detected")
                client.cooldown_account(account["id"], minutes=_FLOOD_COOLDOWN_MINUTES, reason=reason)
                cooled_down = True
                adapters.discard(account, lead["platform"])
            else:
                # Не проблема аккаунта — лид остаётся status=new (можно ретраить в следующем
                # запуске кампании), но в рамках текущего run_send_task больше не берём его,
                # иначе внешний while-цикл будет ретраить его бесконечно.
                task.permanently_failed_lead_ids.add(lead["id"])
    finally:
        if adapter is not None and getattr(adapter, "challenge_passed", False):
            # Человек прошёл антибот-проверку VK вручную — сохраняем обновлённые cookies, чтобы
            # следующий запуск не получал challenge заново со старой сессией.
            state = adapter.storage_state() if hasattr(adapter, "storage_state") else None
            if state:
                try:
                    client.put_session(account["id"], state)
                    logger.info("saved refreshed VK session for account %s after challenge", account["id"])
                except DataServiceError:
                    logger.exception("failed to save refreshed session of account %s", account["id"])
            adapter.challenge_passed = False
        if not cooled_down:
            try:
                client.release_account(account["id"])
            except DataServiceError:
                logger.exception("failed to release account %s", account["id"])

    # Единственная пауза между лидами — внутри send_message адаптеров остались только короткие
    # "дать странице устояться" (см. SEND_SETTLE_*_SEC в config.py).
    ratelimit.delay_for_platform(lead["platform"])
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
