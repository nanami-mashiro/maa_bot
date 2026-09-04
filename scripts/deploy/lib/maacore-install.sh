#!/bin/sh
# 在 maa-bot 镜像内安装 MaaCore 与资源(幂等)。代理由容器 env 提供。
set -eu

log() { echo "[maacore-install] $*"; }

# library 和 resource 都齐才算装完:资源 clone 可能在 core 装完后失败,
# 只查 library 会把半装状态当成功跳过(踩过)。
if [ -n "$(ls -A "$(maa dir library 2>/dev/null)" 2>/dev/null)" ] \
  && [ -n "$(ls -A "$(maa dir resource 2>/dev/null)" 2>/dev/null)" ]; then
  log "MaaCore 已安装($(maa version 2>/dev/null | head -1 || echo unknown)),跳过"
  exit 0
fi

log "安装 MaaCore 与资源(GitHub 下载,可能需要数分钟)"
# 代理链路(容器→xray→镜像测速)偶发 Peer disconnected,重试兜底。
# 首次失败后半装状态会报 "MaaCore already exists",重试须 --force。
# 资源 clone 依赖镜像内的真 git(libgit2 兜底不走代理,解析 github.com 必败)。
n=0
force=""
until maa install stable $force; do
  n=$((n+1))
  [ "$n" -ge 3 ] && { log "错误:maa install 连续 ${n} 次失败"; exit 22; }
  log "maa install 失败(第 ${n} 次),10s 后重试"
  force="--force"
  sleep 10
done
maa version | head -3
log "完成"
