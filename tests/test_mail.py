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
