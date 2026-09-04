# MAA Telegram Bot 一键部署 —— Windows / Docker Desktop (WSL2) 入口。
# 动态探测内核/架构能力,按需:编译带 binder 的 WSL2 内核并切换、
# 构建 libndk 转译 redroid 镜像、生成平台 override、拉起全栈并安装游戏。
# 幂等可重入:任何阶段中断后重跑即从探测续走。
#
# 本文件必须保存为 UTF-8 with BOM(Windows PowerShell 5.1 无 BOM 会按 ANSI 解码中文)。
#
# 用法: powershell -ExecutionPolicy Bypass -File scripts\deploy\deploy.ps1 [-Yes] [-SkipKernel] [-ForceKernelRebuild] [-NoLaunch]
#   -Yes                跳过重启 Docker Desktop 前的人工确认(无人值守)
#   -SkipKernel         跳过内核阶段(仅想构建镜像时用)
#   -ForceKernelRebuild 忽略 bzImage 缓存强制重编
#   -NoLaunch           安装 APK 后不自动启动游戏
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$SkipKernel,
    [switch]$ForceKernelRebuild,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$CacheDir = Join-Path $RepoRoot '.deploy-cache'
$LogDir = Join-Path $CacheDir 'logs'
$KernelStore = Join-Path $env:USERPROFILE 'wsl-kernel'
$WslConfigPath = Join-Path $env:USERPROFILE '.wslconfig'
# docker -v 需要正斜杠形式的 Windows 路径
$RepoMnt = $RepoRoot -replace '\\', '/'
$CacheMnt = $CacheDir -replace '\\', '/'

New-Item -ItemType Directory -Force $CacheDir, $LogDir, $KernelStore | Out-Null
$Transcript = Join-Path $LogDir ("deploy-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
Start-Transcript -Path $Transcript | Out-Null

function Log([string]$msg) { Write-Host "[deploy] $msg" }
function Fail([int]$code, [string]$msg) {
    Write-Host "[deploy] 错误: $msg" -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch {}
    exit $code
}

# 静默执行原生命令:经 cmd 重定向,避免 PS5.1 把 stderr 包装成 ErrorRecord
# 后被 $ErrorActionPreference=Stop 误杀。返回退出码。
function Invoke-Quiet([string]$CommandLine) {
    cmd /c "$CommandLine >nul 2>&1"
    return $LASTEXITCODE
}

function Read-DotEnv {
    $envPath = Join-Path $RepoRoot '.env'
    $map = @{}
    if (Test-Path $envPath) {
        foreach ($line in Get-Content $envPath) {
            if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
            $k, $v = $line -split '=', 2
            $map[$k.Trim()] = $v.Trim()
        }
    }
    return $map
}

function Get-Facts([string[]]$ExtraArgs) {
    $out = & docker run --rm @ExtraArgs -v "${RepoMnt}:/repo:ro" alpine sh -c "tr -d '\r' </repo/scripts/deploy/lib/probe.sh >/tmp/s && sh /tmp/s"
    if ($LASTEXITCODE -ne 0) { return $null }
    $f = @{}
    foreach ($line in $out) { if ($line -match '^([A-Z0-9_]+)=(.*)$') { $f[$Matches[1]] = $Matches[2] } }
    return $f
}

# ---------- P0 preflight ----------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $dockerBin = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'
    if (Test-Path (Join-Path $dockerBin 'docker.exe')) { $env:PATH = "$dockerBin;$env:PATH" }
    else { Fail 2 'docker CLI 不存在,请安装 Docker Desktop' }
}
if ((Invoke-Quiet 'docker info') -ne 0) {
    Log 'Docker 引擎未运行,尝试启动 Docker Desktop...'
    Start-Process (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe')
    $deadline = (Get-Date).AddSeconds(180)
    do { Start-Sleep 5 } until (((Invoke-Quiet 'docker info') -eq 0) -or ((Get-Date) -gt $deadline))
    if ((Invoke-Quiet 'docker info') -ne 0) { Fail 2 'Docker 引擎 180s 内未就绪' }
}
$DotEnv = Read-DotEnv
foreach ($k in 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALLOWED_USER_IDS') {
    if (-not $DotEnv[$k]) { Fail 40 ".env 缺少 $k(compose 强制插值,缺失则所有 compose 命令都会失败)" }
}
$ProxyRaw = $null
foreach ($k in 'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY') { if ($DotEnv[$k]) { $ProxyRaw = $DotEnv[$k]; break } }

# ---------- P1 probe ----------
Log '探测环境(容器内)...'
$probeArgs = @('-v', "${CacheMnt}:/cache")
if ($ProxyRaw) { $probeArgs += @('-e', "PROBE_PROXY=$ProxyRaw") }
$Facts = Get-Facts $probeArgs
if (-not $Facts) { Fail 20 '探针容器执行失败(alpine 镜像拉取受阻?)' }
# busybox wget 走 https 代理不可靠,代理连通性用 curl 镜像单独实测。
$ProxyOk = $false
if ($ProxyRaw) {
    Invoke-Quiet "docker run --rm curlimages/curl -sS -m 12 -x $ProxyRaw -o /dev/null https://api.telegram.org/" | Out-Null
    $ProxyOk = ($LASTEXITCODE -eq 0)
}
Log ("探测结果: arch={0} kernel={1} wsl2={2} binder={3} ashmem={4} proxy_ok={5}" -f `
        $Facts['ARCH'], $Facts['KERNEL'], $Facts['WSL2'], $Facts['BINDER_OK'], $Facts['ASHMEM'], $ProxyOk)

# ---------- P2 内核(WSL2 且无 binder) ----------
function Restart-DockerDesktop {
    Log '停止 Docker Desktop...'
    if ((Invoke-Quiet 'docker desktop stop') -ne 0) {
        Get-Process 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep 5
    Log 'wsl --shutdown'
    Invoke-Quiet 'wsl --shutdown' | Out-Null
    Start-Sleep 3
    Log '启动 Docker Desktop...'
    if ((Invoke-Quiet 'docker desktop start') -ne 0) {
        Start-Process (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe')
    }
    # 非正常停机后的恢复启动可能远超 5 分钟,放宽等待。
    $deadline = (Get-Date).AddSeconds(600)
    do { Start-Sleep 5 } until (((Invoke-Quiet 'docker info') -eq 0) -or ((Get-Date) -gt $deadline))
    return ((Invoke-Quiet 'docker info') -eq 0)
}

function Write-WslConfigKernel([string]$BzPath) {
    # ini 感知合并:只改/插 [wsl2] 段的 kernel= 行,其余保留;无 BOM 写入。
    # kernel= 路径必须转义反斜杠(C:\\Users\\...,微软文档格式)。
    # 实测:单反斜杠不生效(静默回落官方内核),双反斜杠正确。
    $kernelLine = 'kernel=' + ($BzPath -replace '\\', '\\')
    $lines = @()
    if (Test-Path $WslConfigPath) {
        Copy-Item $WslConfigPath "$WslConfigPath.bak-$(Get-Date -Format yyyyMMddHHmmss)"
        $lines = @(Get-Content $WslConfigPath)
    }
    $out = New-Object System.Collections.Generic.List[string]
    $inWsl2 = $false; $written = $false; $hasSection = $false
    foreach ($line in $lines) {
        if ($line -match '^\s*\[(.+)\]\s*$') {
            if ($inWsl2 -and -not $written) { $out.Add($kernelLine); $written = $true }
            $inWsl2 = ($Matches[1] -eq 'wsl2')
            if ($inWsl2) { $hasSection = $true }
            $out.Add($line); continue
        }
        if ($inWsl2 -and $line -match '^\s*kernel\s*=') {
            if (-not $written) { $out.Add($kernelLine); $written = $true }
            continue
        }
        $out.Add($line)
    }
    if ($inWsl2 -and -not $written) { $out.Add($kernelLine); $written = $true }
    if (-not $hasSection) { $out.Add('[wsl2]'); $out.Add($kernelLine) }
    [System.IO.File]::WriteAllText($WslConfigPath, (($out -join "`r`n") + "`r`n"))
}

function Set-DockerDesktopProxy([string]$HostProxy) {
    # 守护进程拉镜像不走容器代理;把代理合并进 Docker Desktop 设置(重启生效)。
    $store = Join-Path $env:APPDATA 'Docker\settings-store.json'
    if (-not (Test-Path $store)) { Log '未找到 settings-store.json,跳过守护进程代理配置'; return }
    Copy-Item $store "$store.bak" -Force
    $json = Get-Content $store -Raw | ConvertFrom-Json
    $json | Add-Member -NotePropertyName 'ProxyHTTPMode' -NotePropertyValue 'manual' -Force
    $json | Add-Member -NotePropertyName 'OverrideProxyHTTP' -NotePropertyValue $HostProxy -Force
    $json | Add-Member -NotePropertyName 'OverrideProxyHTTPS' -NotePropertyValue $HostProxy -Force
    [System.IO.File]::WriteAllText($store, ($json | ConvertTo-Json -Depth 10))
    Log "已写入 Docker Desktop 守护进程代理: $HostProxy(重启后生效)"
}

if ($Facts['BINDER_OK'] -ne 'true' -and -not $SkipKernel) {
    if ($Facts['WSL2'] -ne 'true') { Fail 30 '当前 Docker 引擎内核无 binder 且非 WSL2,请参考 README 为宿主内核启用 binder' }
    $ver = $Facts['KERNEL'] -replace '-microsoft-standard-WSL2.*$', ''
    $bz = Join-Path $KernelStore "bzImage-$ver-binder"

    # 兼容旧命名产物
    $legacy = Join-Path $KernelStore 'bzImage-binder'
    if (-not (Test-Path $bz) -and (Test-Path $legacy)) { Copy-Item $legacy $bz }

    if ($ForceKernelRebuild -or -not (Test-Path $bz)) {
        Log "编译带 binder 的 WSL2 内核 $ver(容器内,约 10-30 分钟)..."
        $kArgs = @('-v', "${CacheMnt}:/cache")
        if ($ProxyRaw) { $kArgs += @('-e', "HTTPS_PROXY=$ProxyRaw", '-e', "HTTP_PROXY=$ProxyRaw") }
        & docker run --rm @kArgs -v "${RepoMnt}:/repo:ro" debian:trixie-slim bash -c "tr -d '\r' </repo/scripts/deploy/lib/build-wsl-kernel.sh >/tmp/s && bash /tmp/s $ver"
        if ($LASTEXITCODE -ne 0) { Fail 22 '内核编译失败,日志见上方输出' }
        Copy-Item (Join-Path $CacheDir "kernel\bzImage-$ver-binder") $bz -Force
    }
    else { Log "命中内核产物缓存: $bz" }

    Write-WslConfigKernel $bz
    Log "已写 .wslconfig kernel= $bz"

    if ($ProxyRaw) {
        # 容器视角代理地址(192.168.65.254/host.docker.internal)换算为宿主回环。
        $hostProxy = $ProxyRaw -replace '192\.168\.65\.254', '127.0.0.1' -replace 'host\.docker\.internal', '127.0.0.1'
        Set-DockerDesktopProxy $hostProxy
    }

    $running = & docker ps --format '{{.Names}} ({{.Image}})'
    Write-Host ''
    Write-Host '即将重启 Docker Desktop 以启用 binder 内核。将中断:' -ForegroundColor Yellow
    if ($running) { $running | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow } }
    else { Write-Host '  (当前无运行中的容器)' -ForegroundColor Yellow }
    Write-Host '  以及所有 WSL 发行版会话。' -ForegroundColor Yellow
    if (-not $Yes) {
        $answer = Read-Host '继续?[y/N]'
        if ($answer -notmatch '^[yY]') { Log '已取消。稍后可重跑本脚本继续(探测到 binder 缺失会再次走到这里)'; try { Stop-Transcript | Out-Null } catch {}; exit 10 }
    }

    if (-not (Restart-DockerDesktop)) {
        Log '重启后 Docker 未就绪,回滚 .wslconfig 到最近备份...'
        # 只认本脚本的时间戳备份(bak-yyyyMMddHHmmss);字符串排序会把
        # 带字母后缀的手工备份排到最后,导致"回滚"回错文件(踩过)。
        $bak = Get-ChildItem "$WslConfigPath.bak-*" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '\.bak-\d{14}$' } | Sort-Object Name | Select-Object -Last 1
        if ($bak) { Copy-Item $bak.FullName $WslConfigPath -Force } else { Remove-Item $WslConfigPath -Force }
        if (Restart-DockerDesktop) { Fail 11 '自编内核不可用,已回滚到官方内核。产物与日志保留在 wsl-kernel/.deploy-cache 供排查' }
        Fail 12 "回滚后 Docker 仍未就绪。手动恢复:1) 还原/删除 $WslConfigPath 中 kernel= 行 2) wsl --shutdown 3) 重启 Docker Desktop"
    }

    Log '复验 binder...'
    $Facts = Get-Facts @()
    if (-not $Facts -or $Facts['BINDER_OK'] -ne 'true') {
        & docker run --rm alpine sh -c 'uname -r; zcat /proc/config.gz | grep -i binder'
        Fail 13 'Docker 已就绪但 binder 仍缺失:kernel= 可能未生效(路径/编码)或 config 未包含 binder,见上方诊断输出'
    }
    Log 'binder 已启用 ✓'
}
elseif ($Facts['BINDER_OK'] -eq 'true') { Log 'binder 已可用,跳过内核阶段' }

# ---------- P3 redroid 镜像 ----------
if ($Facts['ARCH'] -eq 'amd64') {
    if ((Invoke-Quiet 'docker image inspect redroid/redroid:11.0.0_ndk') -ne 0) {
        Log '构建 redroid 11.0.0 + libndk 转译镜像(含基础镜像拉取,耗时较长)...'
        $nArgs = @('-v', '/var/run/docker.sock:/var/run/docker.sock', '-v', "${CacheMnt}:/cache")
        if ($ProxyRaw) { $nArgs += @('-e', "HTTPS_PROXY=$ProxyRaw", '-e', "HTTP_PROXY=$ProxyRaw") }
        & docker run --rm @nArgs -v "${RepoMnt}:/repo:ro" docker:cli sh -c "tr -d '\r' </repo/scripts/deploy/lib/redroid-image.sh >/tmp/s && sh /tmp/s"
        if ($LASTEXITCODE -ne 0) { Fail 22 'redroid ndk 镜像构建失败(拉取停滞时请在 Docker Desktop Settings→Resources→Proxies 配置代理后重跑)' }
    }
    else { Log 'redroid ndk 镜像已存在,跳过' }
}
else {
    if ((Invoke-Quiet 'docker image inspect redroid/redroid:11.0.0-latest') -ne 0) {
        & docker pull redroid/redroid:11.0.0-latest
        if ($LASTEXITCODE -ne 0) { Fail 20 'redroid 镜像拉取失败' }
    }
}

# ---------- P4 生成 override ----------
Log '生成 docker-compose.override.yml...'
$oArgs = @('-e', "FACT_ARCH=$($Facts['ARCH'])", '-e', "FACT_ASHMEM=$($Facts['ASHMEM'])")
$override = (& docker run --rm @oArgs -v "${RepoMnt}:/repo:ro" alpine sh -c "tr -d '\r' </repo/scripts/deploy/lib/gen-override.sh >/tmp/s && sh /tmp/s") -join "`n"
if ($LASTEXITCODE -ne 0 -or -not $override) { Fail 22 'override 生成失败' }
$overridePath = Join-Path $RepoRoot 'docker-compose.override.yml'
$existing = ''
if (Test-Path $overridePath) { $existing = (Get-Content $overridePath -Raw) }
if ($existing.Replace("`r`n", "`n").Trim() -ne $override.Trim()) {
    [System.IO.File]::WriteAllText($overridePath, $override + "`n")
    Log 'override 已更新'
}
else { Log 'override 无变化' }
Push-Location $RepoRoot
& docker compose config -q
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail 40 'compose 配置校验失败(.env/override)' }

# ---------- P5 构建 maa-bot ----------
# 不再向 compose build 导出代理 env:客户端 env 会被 buildkitd 在 VM 根
# netns 里拨号,而 192.168.65.254 只在容器网络可达 → proxyconnect 超时(踩过)。
# 镜像拉取与 RUN 步骤出网统一由 Docker Desktop 守护进程代理(P2 已写入)承担。
Log '构建 maa-bot 镜像...'
& docker compose build maa-bot
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail 22 'maa-bot 构建失败' }

# ---------- P6 起 redroid 并等待就绪 ----------
Log '启动 redroid...'
& docker compose up -d redroid
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail 21 'redroid 启动失败' }
$libMntArgs = @('-v', "${RepoMnt}/scripts/deploy/lib:/deploylib:ro", '-v', "${CacheMnt}:/cache")
& docker compose run --rm --no-deps @libMntArgs maa-bot sh -c "tr -d '\r' </deploylib/android-ready.sh >/tmp/s && sh /tmp/s"
if ($LASTEXITCODE -ne 0) {
    cmd /c "docker logs --tail 40 redroid 2>&1"
    Pop-Location; Fail 21 'redroid 未在超时内完成启动,见上方容器日志'
}

# ---------- P7 安装并启动游戏 ----------
$gEnv = ''
if ($DotEnv['CLIENT_UPDATE_DOWNLOAD_URL']) { $gEnv = "APK_URL='$($DotEnv['CLIENT_UPDATE_DOWNLOAD_URL'])' " }
if ($NoLaunch) { $gEnv += 'SKIP_LAUNCH=true ' }
& docker compose run --rm --no-deps @libMntArgs maa-bot sh -c "tr -d '\r' </deploylib/game-setup.sh >/tmp/s && ${gEnv}sh /tmp/s"
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail 23 '游戏安装/启动验证失败' }

# ---------- P8 MaaCore ----------
& docker compose run --rm --no-deps @libMntArgs maa-bot sh -c "tr -d '\r' </deploylib/maacore-install.sh >/tmp/s && sh /tmp/s"
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail 22 'MaaCore 安装失败' }

# ---------- P9 起 bot 并终检 ----------
Log '启动 maa-bot...'
& docker compose up -d
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail 22 'maa-bot 启动失败' }
$verify = & docker compose run --rm --no-deps @libMntArgs maa-bot sh -c "tr -d '\r' </deploylib/verify.sh >/tmp/s && sh /tmp/s"
Pop-Location
Write-Host ''
Write-Host '================ 部署摘要 ================' -ForegroundColor Green
$verify | ForEach-Object { Write-Host "  $_" }
Write-Host '==========================================' -ForegroundColor Green
Write-Host '剩余人工步骤:首次账号登录如遇验证码无法自动化,'
Write-Host '  在 Windows 上 adb connect 127.0.0.1:5555 后用 scrcpy 手动完成一次登录;'
Write-Host '  之后 redroid-data 卷会保持登录态。Telegram 里发送 /status 验证 bot。'
try { Stop-Transcript | Out-Null } catch {}
