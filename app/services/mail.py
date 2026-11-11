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
    # Порт 465 — шифрование с первого байта (SMTPS); 587 и прочие —
    # обычное соединение, переводимое в шифрованное командой STARTTLS.
    # STARTTLS на 465 не работает: сервер ждёт TLS-рукопожатия и молчит.
    if settings.smtp_port == 465:
        server = smtplib.SMTP_SSL(
            settings.smtp_server, settings.smtp_port, timeout=20, context=context
        )
    else:
        server = smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=20)
    with server:
        server.ehlo()
        if settings.smtp_port != 465:
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

    # Каждое письмо оставляет в журнале одну строку с исходом. Раньше
    # успешная отправка не писала ничего, а заблокированная — незаметную
    # строку, и на вопрос «почему не пришло письмо» журнал не отвечал.
    if settings.mail_dry_run or not settings.smtp_server:
        logger.info("ПОЧТА не отправлено (тестовый режим, MAIL_DRY_RUN) → %s: %s", to, subject)
        return True

    allowlist = settings.mail_allowlist
    if allowlist and to.strip().lower() not in allowlist:
        logger.warning(
            "ПОЧТА не отправлено (адреса нет в MAIL_ALLOWED_RECIPIENTS) → %s: %s", to, subject
        )
        return False

    for attempt in range(1, _ATTEMPTS + 1):
        try:
            # Блокирующий smtplib уводится с цикла событий, иначе он
            # останавливает обработку всех остальных запросов.
            await asyncio.to_thread(_send_sync, to, subject, body)
            logger.info("ПОЧТА отправлено → %s: %s", to, subject)
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


def describe_mode() -> str:
    """Режим почты одной строкой — печатается при запуске сервера."""
    if settings.mail_dry_run:
        return "ПОЧТА: тестовый режим (MAIL_DRY_RUN=true) — письма только пишутся в журнал"
    if not settings.smtp_server:
        return "ПОЧТА: SMTP_SERVER не задан — письма только пишутся в журнал"
    if not settings.smtp_password and settings.smtp_user:
        return (
            f"ПОЧТА: {settings.smtp_server}:{settings.smtp_port} — "
            "SMTP_PASSWORD пуст, сервер не примет вход"
        )
    allowlist = settings.mail_allowlist
    limit = (
        f"только на: {', '.join(sorted(allowlist))}" if allowlist else "на любые адреса"
    )
    return (
        f"ПОЧТА: отправка через {settings.smtp_server}:{settings.smtp_port} "
        f"от {settings.mail_from}, {limit}"
    )


async def send_outbox(items: list[OutboxItem], *, author_email: str | None = None) -> None:
    """
    Отправляет подготовленные машиной состояний письма.

    Письма автору собираются с пустым получателем: сервис согласования знает
    только идентификатор автора, но не его почту. Адрес подставляется здесь.
    """
    for item in items:
        to = item.to or (author_email or "")
        await send_one(to, item.subject, item.body)
