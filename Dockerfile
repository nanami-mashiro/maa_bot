FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:/root/.local/bin:${PATH}" \
    MAA_CONFIG_DIR="/data/maa-config" \
    BOT_LOG_DIR="/data/logs"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        adb \
        ca-certificates \
        curl \
        git \
        libatomic1 \
        libgomp1 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://raw.githubusercontent.com/MaaAssistantArknights/maa-cli/main/install.sh | bash

ENV TZ="Asia/Shanghai"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY maa_tg_bot ./maa_tg_bot
COPY scripts/entrypoint.sh /entrypoint.sh

# 防御 Windows CRLF checkout:入口脚本必须是 LF,否则 shebang 带 \r 无法执行。
RUN sed -i 's/\r$//' /entrypoint.sh \
    && uv sync --frozen --no-dev \
    && chmod 755 /entrypoint.sh

VOLUME ["/data/maa-config", "/data/logs"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["maa-tg-bot"]
