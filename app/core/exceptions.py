"""Стандартные ошибки API. Перенесено из EDMS без изменений поведения."""

from fastapi import HTTPException
from starlette import status


def http_400(msg: str = "Некорректный запрос") -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)


def http_401(msg: str = "Требуется авторизация", headers: dict | None = None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=msg,
        headers=headers or {"WWW-Authenticate": "Bearer"},
    )


def http_403(msg: str = "Доступ запрещён") -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)


def http_404(msg: str = "Объект не найден") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


def http_409(msg: str = "Конфликт состояния") -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)


def http_410(msg: str = "Ресурс больше не доступен") -> HTTPException:
    return HTTPException(status_code=status.HTTP_410_GONE, detail=msg)


def http_422(msg: str = "Данные не прошли проверку") -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=msg)


def http_500(msg: str = "Внутренняя ошибка сервера") -> HTTPException:
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg)
