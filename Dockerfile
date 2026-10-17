FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

# Build with --build-arg WITH_BOOKING=true to include the browser for "hold in cart".
ARG WITH_BOOKING=false

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    CAMPWATCH_DATA_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN EXTRAS=$([ "$WITH_BOOKING" = "true" ] && echo "--extra booking"); \
    uv sync --frozen --no-dev --no-install-project $EXTRAS

COPY campwatch ./campwatch
RUN EXTRAS=$([ "$WITH_BOOKING" = "true" ] && echo "--extra booking"); \
    uv sync --frozen --no-dev $EXTRAS && \
    if [ "$WITH_BOOKING" = "true" ]; then uv run --no-sync playwright install --with-deps chromium; fi

VOLUME /data
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "campwatch", "--host", "0.0.0.0", "--port", "8000"]
