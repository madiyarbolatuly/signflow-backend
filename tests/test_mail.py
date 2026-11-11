"""Отправка почты: тестовый режим и список разрешённых адресов."""

import pytest

from app.services import mail


@pytest.fixture
def sent(monkeypatch):
    """Вместо настоящего SMTP — запись того, что ушло бы."""
    out: list[str] = []
    monkeypatch.setattr(mail, "_send_sync", lambda to, subject, body: out.append(to))
    monkeypatch.setattr(mail.settings, "smtp_server", "smtp.example.com")
    monkeypatch.setattr(mail.settings, "mail_dry_run", False)
    return out


async def test_dry_run_sends_nothing(sent, monkeypatch):
    monkeypatch.setattr(mail.settings, "mail_dry_run", True)
    assert await mail.send_one("a@gqgroup.kz", "s", "b") is True
    assert sent == []


async def test_allowlist_lets_only_listed_addresses_through(sent, monkeypatch):
    monkeypatch.setattr(mail.settings, "mail_allowed_recipients", " Me@NU.edu.kz , other@x.kz")

    assert await mail.send_one("me@nu.edu.kz", "s", "b") is True
    assert await mail.send_one("colleague@gqgroup.kz", "s", "b") is False

    assert sent == ["me@nu.edu.kz"], "коллега из тестового маршрута не должен получить письмо"


async def test_empty_allowlist_means_no_restriction(sent, monkeypatch):
    monkeypatch.setattr(mail.settings, "mail_allowed_recipients", "")
    assert await mail.send_one("anyone@gqgroup.kz", "s", "b") is True
    assert sent == ["anyone@gqgroup.kz"]


def test_mode_line_says_what_will_happen(monkeypatch):
    s = mail.settings
    monkeypatch.setattr(s, "mail_dry_run", True)
    assert "тестовый режим" in mail.describe_mode()

    monkeypatch.setattr(s, "mail_dry_run", False)
    monkeypatch.setattr(s, "smtp_server", "smtp.gmail.com")
    monkeypatch.setattr(s, "smtp_user", "me@gmail.com")
    monkeypatch.setattr(s, "smtp_password", "")
    assert "SMTP_PASSWORD пуст" in mail.describe_mode()

    monkeypatch.setattr(s, "smtp_password", "secret")
    monkeypatch.setattr(s, "mail_allowed_recipients", "me@nu.edu.kz")
    line = mail.describe_mode()
    assert "smtp.gmail.com" in line and "только на: me@nu.edu.kz" in line
    assert "secret" not in line, "пароль в журнал не попадает"


async def test_every_outcome_is_logged(sent, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="signflow.mail")
    monkeypatch.setattr(mail.settings, "mail_allowed_recipients", "ok@x.kz")
    await mail.send_one("ok@x.kz", "Тема", "b")
    await mail.send_one("blocked@x.kz", "Тема", "b")
    text = caplog.text
    assert "ПОЧТА отправлено → ok@x.kz" in text
    assert "не отправлено (адреса нет в MAIL_ALLOWED_RECIPIENTS) → blocked@x.kz" in text

