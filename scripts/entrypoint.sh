#!/usr/bin/env sh
set -eu

mkdir -p "${MAA_CONFIG_DIR:-/data/maa-config}/tasks"
mkdir -p "${MAA_CONFIG_DIR:-/data/maa-config}/profiles"
mkdir -p "${BOT_LOG_DIR:-/data/logs}"

if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
  ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
  printf '%s\n' "${TZ}" > /etc/timezone
fi

if [ ! -f "${MAA_CONFIG_DIR:-/data/maa-config}/profiles/default.toml" ] \
  && [ -f /app/config/maa-profile.toml ]; then
  cp /app/config/maa-profile.toml "${MAA_CONFIG_DIR:-/data/maa-config}/profiles/default.toml"
fi

if [ -f "${MAA_CONFIG_DIR:-/data/maa-config}/profiles/default.toml" ] \
  && [ "${ADB_SERIAL:-}" = "127.0.0.1:5555" ] \
  && grep -q 'address = "redroid:5555"' "${MAA_CONFIG_DIR:-/data/maa-config}/profiles/default.toml"; then
  sed -i 's/address = "redroid:5555"/address = "127.0.0.1:5555"/' \
    "${MAA_CONFIG_DIR:-/data/maa-config}/profiles/default.toml"
fi

if [ -f "${MAA_CONFIG_DIR:-/data/maa-config}/profiles/default.toml" ] \
  && grep -q 'touch_mode = "MaaTouch"' "${MAA_CONFIG_DIR:-/data/maa-config}/profiles/default.toml"; then
  sed -i 's/touch_mode = "MaaTouch"/touch_mode = "ADB"/' \
    "${MAA_CONFIG_DIR:-/data/maa-config}/profiles/default.toml"
fi

exec "$@"
