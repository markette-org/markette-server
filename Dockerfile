FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# git + ssh are needed by the Hugo provider.
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN DJANGO_SECRET_KEY=build python manage.py collectstatic --noinput \
    && useradd --create-home --uid 1000 markette \
    && mkdir -p /app/media /app/var/hugo && chown -R markette /app/media /app/var
USER markette

EXPOSE 8000
CMD ["gunicorn", "markette.wsgi", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
