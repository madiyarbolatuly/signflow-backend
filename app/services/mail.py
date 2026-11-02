"""
Отправка почты.

Исправляет риск Р2 из ТЗ. В EDMS `mail_service()` вызывал `smtplib` прямо в
обработчике запроса: недоступный SMTP означал запрос, висящий до таймаута,
и занятый всё это время воркер. Почта в SignFlow находится на критическом
пути — без письма согласующий не узнает о документе, — поэтому отправка
вынесена в отдельный поток и вызывается уже после фиксации транзакции, через
`BackgroundTasks`.
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import settings
from app.services.approval import OutboxItem

logger = logging.getLogger("signflow.mail")

#: Сколько раз повторить отправку, прежде чем признать письмо недоставленным.
_ATTEMPTS = 3
#: Пауза между попытками, секунды. Растёт линейно: 2, 4, 6.
_BACKOFF_STEP = 2


def _send_sync(to: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.mail_from
    message["To"] = to
    message.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=20) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)


async def send_one(to: str, subject: str, body: str) -> bool:
    """
    Отправляет одно письмо. Возвращает признак успеха, исключение не бросает.

    Отказ почтового сервера не должен приводить к отказу в обслуживании
    (ТЗ, п. 5.1.5), поэтому ошибка только пишется в журнал.
    """
    if not to:
        logger.warning("Письмо без получателя пропущено: %s", subject)
        return False

    if settings.mail_dry_run or not settings.smtp_server:
        logger.info("[dry-run] письмо для %s: %s", to, subject)
        return True

    for attempt in range(1, _ATTEMPTS + 1):
        try:
            # Блокирующий smtplib уводится с цикла событий, иначе он
            # останавливает обработку всех остальных запросов.
            await asyncio.to_thread(_send_sync, to, subject, body)
            return True
        except Exception as exc:  # noqa: BLE001 — журналируем любую причину
            logger.warning(
                "Не удалось отправить письмо для %s (попытка %d из %d): %s",
                to,
                attempt,
                _ATTEMPTS,
                exc,
            )
            if attempt < _ATTEMPTS:
                await asyncio.sleep(_BACKOFF_STEP * attempt)

    logger.error("Письмо для %s не доставлено: %s", to, subject)
    return False


async def send_outbox(items: list[OutboxItem], *, author_email: str | None = None) -> None:
    """
    Отправляет подготовленные машиной состояний письма.

    Письма автору собираются с пустым получателем: сервис согласования знает
    только идентификатор автора, но не его почту. Адрес подставляется здесь.
    """
    for item in items:
        to = item.to or (author_email or "")
        await send_one(to, item.subject, item.body)
