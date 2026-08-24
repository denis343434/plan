import logging
from dataclasses import dataclass, field
from enum import StrEnum

from app.adapters.base import SessionExpiredError
from app.adapters.registry import get_inbox_adapter
from app.clients.data_service import DataServiceClient
from app.exceptions import DataServiceError

logger = logging.getLogger(__name__)

# Раньше здесь стоял конечный лимит (20, потом 100) на один клик "Проверить ответы". Любое
# конечное число ломается так же, только позже: как только у ВСЕХ pending-сообщений
# reply_status уже не "none" (каждое хоть раз проверено), сортировка по sent_at внутри группы
# становится статичной — один и тот же батч из самых старых "no_reply" клик за кликом
# перепроверяется заново, а более новые (например реальный неотвеченный ЧЕБЕР/Осьминожка)
# никогда не доходят до проверки, пока старые не станут "replied" и не освободят место. При
# 105+ сообщениях на аккаунт это воспроизводится снова. limit=None — проверяем весь pending
# аккаунта целиком за один клик, лимит остаётся только как ручной параметр на крайний случай.
_DEFAULT_CHECK_LIMIT = None


class InboxCheckStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


@dataclass
class InboxCheckResultItem:
    message_id: str
    lead_id: str
    has_reply: bool
    preview: str | None = None
    error: str | None = None


@dataclass
class InboxCheckTask:
    account_id: str
    status: InboxCheckStatus = InboxCheckStatus.queued
    # checked/total растут по ходу проверки (см. on_progress ниже) — десктоп-клиент опрашивает
    # задачу, пока она не done/failed, вместо одного долгого блокирующего HTTP-запроса. Раньше
    # был именно такой запрос (POST /inbox/check ждал ответа целиком) — при реальном VK-аккаунте
    # с десятками сообщений это легко занимало по нескольку минут и выглядело как зависание.
    checked: int = 0
    total: int = 0
    replied: int = 0
    results: list[InboxCheckResultItem] = field(default_factory=list)
    error: str | None = None
    # True, только если вызывающий явно передал конечный limit и pending-очередь оказалась
    # больше него — десктоп-клиент по умолчанию лимит не передаёт (см. limit=None в
    # run_inbox_check_task), тогда весь pending аккаунта уходит в один батч и has_more всегда
    # False. Флаг остаётся на случай, если limit когда-нибудь передадут явно (см. историю: с
    # конечным limit десктоп-клиент опирался на has_more, чтобы самому дочищать очередь).
    has_more: bool = False
    # True, если остановились именно из-за протухшей VK-сессии (см. SessionExpiredError) — не
    # просто "какая-то ошибка", а конкретно "нужен повторный вход". Десктоп-клиент показывает
    # это не всплывающим окном (см. запрос пользователя 2026-08-24 — "ошибка должна быть в
    # приложении, а не окном винды"), а прямо статусом сессии этого аккаунта в таблице.
    session_expired: bool = False


# Ключ — account_id: за один момент времени на аккаунт имеет смысл только одна проверка
# (тот же браузерный контекст/сессия), как campaign_id -> SendTask в tasks.py.
TASKS: dict[str, InboxCheckTask] = {}


def create_task(account_id: str) -> InboxCheckTask:
    task = InboxCheckTask(account_id=account_id)
    TASKS[account_id] = task
    return task


def get_task(account_id: str) -> InboxCheckTask | None:
    return TASKS.get(account_id)


def run_inbox_check_task(account_id: str, limit: int | None = _DEFAULT_CHECK_LIMIT) -> None:
    """Проверяет входящие ответы на уже отправленные этим аккаунтом сообщения (фоновая задача).

    Берёт сообщения этого аккаунта со status=sent, у которых ещё не отмечен ответ
    (reply_status != 'replied' — 'no_reply' тоже перепроверяем, ответ мог прийти позже),
    открывает переписку с каждым лидом и смотрит, есть ли новое входящее сообщение. Найденные
    ответы пишет в Data Service (messages.reply_status/reply_preview, leads.status=replied).
    """
    task = TASKS[account_id]
    task.status = InboxCheckStatus.running

    client = DataServiceClient()
    try:
        messages = client.list_messages(account_id=account_id)
        pending = [m for m in messages if m["delivery_status"] == "sent" and m["reply_status"] != "replied"]
        # list_messages отдаёт сообщения по возрастанию sent_at (самые старые первые) — простой
        # pending[:limit] раз за разом резал бы одну и ту же голову списка: проверенное "no_reply"
        # сообщение остаётся в pending (ответ мог прийти позже) и по-прежнему самое старое, поэтому
        # свежие "none" (ещё ни разу не проверенные) сообщения из хвоста списка никогда не доходили
        # бы до лимита. Ставим ещё не проверенные вперёд — стабильная сортировка сохраняет
        # sent_at-порядок внутри каждой группы.
        pending.sort(key=lambda m: m["reply_status"] != "none")
        # has_more должен означать "есть ЕЩЁ НЕ ОХВАЧЕННАЯ свежая работа", а не "весь пул pending
        # больше лимита" — "no_reply" никогда не уходит из pending (перепроверяем на случай
        # позднего ответа, см. докстринг выше), поэтому в стабильном состоянии он почти всегда
        # больше limit. Если считать has_more по всему pending, автопродолжение в десктоп-клиенте
        # зацикливается НАВСЕГДА, гоняя один и тот же no_reply-пул по кругу (подтверждено вживую —
        # один клик "Проверить ответы" перевалил за 360 проверок при паре десятков сообщений).
        # Считаем только "none" (ни разу не проверенные) — конечный, реально убывающий бэклог.
        # limit=None (дефолт) означает "без лимита" — has_more всегда False, резать нечего.
        if limit is None:
            task.has_more = False
        else:
            never_checked = sum(1 for m in pending if m["reply_status"] == "none")
            task.has_more = never_checked > limit
            pending = pending[:limit]
        if not pending:
            task.status = InboxCheckStatus.done
            return

        # /leads не отдаёт выборку по списку id, только limit-пагинацию (см. GET /leads в Data
        # Service) — join делаем на своей стороне, как и десктоп-клиент в _load_messages.
        leads_by_id = {lead["id"]: lead for lead in client.list_leads(limit=1000)}

        checkable: list[tuple[dict, dict]] = []
        for message in pending:
            lead = leads_by_id.get(message["lead_id"])
            if lead is None or not lead.get("group_url"):
                continue
            checkable.append((message, lead))

        task.total = len(checkable)
        if not checkable:
            task.status = InboxCheckStatus.done
            return

        session = client.get_session(account_id)
        platform = checkable[0][1].get("platform", "vk")
        adapter = get_inbox_adapter(platform, storage_state=session["storage_state"])

        def on_progress(checked: int, _total: int) -> None:
            task.checked = checked

        results_by_lead = adapter.check_replies(
            [lead for _, lead in checkable], on_progress=on_progress
        )

        for message, lead in checkable:
            result = results_by_lead.get(lead["id"])
            if result is None:
                continue

            if result.has_reply:
                client.update_message_reply(message["id"], "replied", result.preview)
                try:
                    client.patch_lead_status(lead["id"], "replied")
                except DataServiceError:
                    logger.exception("failed to mark lead %s as replied", lead["id"])
                task.replied += 1
            elif result.error is None:
                client.update_message_reply(message["id"], "no_reply")

            task.results.append(
                InboxCheckResultItem(
                    message_id=message["id"],
                    lead_id=lead["id"],
                    has_reply=result.has_reply,
                    preview=result.preview,
                    error=result.error,
                )
            )

        task.checked = task.total
        task.status = InboxCheckStatus.done

    except DataServiceError as exc:
        task.status = InboxCheckStatus.failed
        task.error = str(exc)
    except SessionExpiredError as exc:
        task.status = InboxCheckStatus.failed
        task.error = str(exc)
        task.session_expired = True
    except Exception as exc:  # noqa: BLE001 - не даём задаче зависнуть на "running" навсегда
        logger.exception("unhandled error in inbox check task %s", account_id)
        task.status = InboxCheckStatus.failed
        task.error = f"internal error: {exc}"
    finally:
        client.close()
