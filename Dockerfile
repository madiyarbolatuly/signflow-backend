# Python 3.12, как указано в ТЗ, п. 5.2.2. Код при этом совместим и с 3.14 —
# из зависимостей убран passlib, который на 3.13+ не импортируется вообще.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Шрифт с кириллицей. Встроенные в reportlab шрифты знают только Latin-1,
# поэтому без этого пакета русский текст в PDF выходит набором точек.
# Проверяется тестом test_cyrillic_font_is_available.
RUN apt-get update     && apt-get install -y --no-install-recommends fonts-dejavu-core     && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# Зависимости отдельным слоем: правка кода не должна вызывать повторную
# установку пакетов.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

RUN useradd --create-home --uid 10001 signflow && chown -R signflow:signflow /srv
USER signflow

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
