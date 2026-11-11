"""
Проверка почты: одно письмо на указанный адрес с текущими настройками.

    python -m app.scripts.send_test_mail madiyar.saduakas@nu.edu.kz

Отвечает, дошло ли письмо до почтового сервера, и если нет — почему:
неверный пароль, закрытый порт, недоступный сервер. Запускать до проверки
всего сценария: так ошибка настройки почты не путается с ошибкой маршрута.
"""

import argparse
import asyncio
import smtplib
import socket

from app.core.config import settings
from app.services.mail import _send_sync


def main() -> None:
    parser = argparse.ArgumentParser(description="Тестовое письмо")
    parser.add_argument("to", help="адрес получателя")
    args = parser.parse_args()

    print(f"сервер: {settings.smtp_server or '(не задан)'}:{settings.smtp_port}")
    print(f"отправитель: {settings.mail_from}, вход как: {settings.smtp_user or '(без входа)'}")
    print(f"MAIL_DRY_RUN: {settings.mail_dry_run}")
    if settings.mail_allowlist:
        print(f"разрешённые адреса: {', '.join(sorted(settings.mail_allowlist))}")

    if not settings.smtp_server:
        raise SystemExit("SMTP_SERVER не задан в .env — отправлять некуда")
    if settings.mail_allowlist and args.to.lower() not in settings.mail_allowlist:
        raise SystemExit(f"{args.to} нет в MAIL_ALLOWED_RECIPIENTS — письмо не уйдёт")

    try:
        asyncio.run(asyncio.to_thread(
            _send_sync,
            args.to,
            "УчетЭДО: проверка почты",
            "Это тестовое письмо УчетЭДО. Если вы его видите, почта настроена верно.",
        ))
    except smtplib.SMTPAuthenticationError as exc:
        raise SystemExit(
            f"сервер не принял логин или пароль: {exc.smtp_code} "
            f"{exc.smtp_error.decode(errors='replace')}\n"
            "Для Gmail и Microsoft 365 нужен пароль приложения, а не обычный пароль."
        )
    except (socket.timeout, TimeoutError, ConnectionRefusedError, OSError) as exc:
        raise SystemExit(f"не удалось подключиться к серверу: {exc}")
    print(f"отправлено на {args.to} — проверьте ящик, включая «Спам»")


if __name__ == "__main__":
    main()
