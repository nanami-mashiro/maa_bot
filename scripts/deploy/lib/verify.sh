#!/bin/sh
# 在 maa-bot 镜像内做终检,输出 KEY=VALUE 摘要。
# 必须在容器内执行:Telegram 需经容器可达的代理;adb 需项目网络。
set -eu

TARGET="${ADB_TARGET:-redroid:5555}"

tg=fail
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  body="$(curl -fsS -m 20 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null || true)"
  case "$body" in *'"ok":true'*) tg=ok ;; esac
fi

adb connect "$TARGET" >/dev/null 2>&1 || true
adbstate="$(adb -s "$TARGET" get-state 2>/dev/null | tr -d '\r\n' || echo none)"

game=absent
if adb -s "$TARGET" shell pm list packages com.hypergryph.arknights 2>/dev/null | grep -q hypergryph; then
  game=installed
fi

maacore=absent
if maa dir library >/dev/null 2>&1 && [ -n "$(ls -A "$(maa dir library)" 2>/dev/null)" ]; then
  maacore="$(maa version 2>/dev/null | head -1 | tr ' ' '_' || echo ok)"
fi

echo "TELEGRAM=$tg"
echo "ADB_STATE=$adbstate"
echo "GAME=$game"
echo "MAACORE=$maacore"
