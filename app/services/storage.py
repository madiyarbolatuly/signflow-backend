"""
Файловое хранилище.

Защита от выхода за корень хранилища перенесена из
`app/services/versioning.py` EDMS: там идентификатор документа подставлялся
в имя файла напрямую, и значение вида `../../etc/foo` писало за пределы
каталога загрузок.
"""

from pathlib import Path

from app.core.config import settings
from app.core.exceptions import http_400


def storage_root() -> Path:
    root = Path(settings.storage_path)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def resolve_within_storage(relative_path: str) -> Path:
    """Превращает хранимый путь в абсолютный, отказывая в выходе за корень."""
    root = storage_root()
    candidate = (root / str(relative_path).lstrip("/\\")).resolve()
    if not candidate.is_relative_to(root):
        raise http_400(f"Путь выходит за пределы хранилища: {relative_path}")
    return candidate


def safe_file_name(name: str) -> str:
    """
    Оставляет от присланного имени только имя файла.

    Браузер может прислать `../../../etc/passwd` в поле имени; `Path.name`
    отсекает всё, кроме последнего сегмента.
    """
    cleaned = Path(name.replace("\\", "/")).name.strip()
    if not cleaned or cleaned in {".", ".."}:
        raise http_400("Недопустимое имя файла")
    return cleaned[:255]


def save_bytes(relative_path: str, data: bytes) -> Path:
    target = resolve_within_storage(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def read_bytes(relative_path: str) -> bytes:
    return resolve_within_storage(relative_path).read_bytes()
