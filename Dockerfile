FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY bot ./bot
RUN pip install --upgrade pip && pip install .

RUN mkdir -p /app/data && chown -R nobody:nogroup /app

USER nobody

CMD ["python", "-m", "bot"]
