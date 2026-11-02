"""
Конфигурация SignFlow.

Отличие от EDMS: там `GlobalConfig` звал `int(os.environ.get("POSTGRES_PORT"))`
на уровне модуля, поэтому отсутствие одной переменной окружения валило процесс
на импорте с `TypeError` вместо внятного сообщения. Здесь всё читает
pydantic-settings: пропущенная обязательная переменная даёт список недостающих
полей, а у необязательных есть значения по умолчанию.
"""

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ─── приложение ──────────────────────────────────────────────────────────
    title: str = "SignFlow"
    description: str = "Электронное согласование и подписание служебных записок"
    version: str = "1.0.0"
    api_prefix: str = "/v1"
    debug: bool = False

    # ─── база данных ─────────────────────────────────────────────────────────
    postgres_user: str = "signflow"
    postgres_password: str = "signflow"
    postgres_hostname: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "signflow"

    #: Задан — используется как есть, в обход полей выше. Нужен тестам
    #: (aiosqlite) и площадкам, выдающим готовую строку подключения.
    database_url_override: str | None = None

    # ─── токены доступа ──────────────────────────────────────────────────────
    jwt_secret_key: str = Field(min_length=16)
    algorithm: str = "HS256"
    access_token_expire_min: int = 720

    # ─── ссылки подписания ───────────────────────────────────────────────────
    #: Срок действия ссылки подписания. ТЗ, п. 5.1.4: по умолчанию 14 суток.
    sign_link_ttl_days: int = 14

    #: Адрес, относительно которого собираются ссылки подписания. Должен быть
    #: доступен получателю письма, поэтому это публичный адрес развёртывания —
    #: никогда не localhost, даже если приложение открывают с той же машины.
    frontend_url: str = "http://localhost:8088"

    # ─── почта ───────────────────────────────────────────────────────────────
    smtp_server: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = "signflow@example.com"
    #: Выключает фактическую отправку: письма только пишутся в журнал. Режим
    #: разработки и тестов.
    mail_dry_run: bool = True

    # ─── файловое хранилище ──────────────────────────────────────────────────
    storage_path: str = "./storage"

    cors_origins_env: str = Field(default="", alias="CORS_ORIGINS")

    @computed_field
    @property
    def cors_origins(self) -> list[str]:
        if self.cors_origins_env.strip():
            return [
                o.strip().rstrip("/")
                for o in self.cors_origins_env.split(",")
                if o.strip()
            ]
        return ["http://localhost:8088", "http://127.0.0.1:8088"]

    @computed_field
    @property
    def async_database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_hostname}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
