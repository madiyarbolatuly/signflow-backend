"""Декларативная база и общие помощники для таблиц."""

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, mapped_column


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def created_at_column():
    """
    Колонка времени создания.

    `default` обязан быть вызываемым. В EDMS несколько таблиц были объявлены
    как `default=datetime.now(timezone.utc)` — выражение вычислялось один раз
    при импорте модуля, поэтому все строки получали время запуска процесса,
    а сортировка по дате создания теряла смысл.
    """
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
