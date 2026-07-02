# MAA Telegram Bot Server

这个项目把 `maa-cli` 封装成一个 Telegram Bot 服务，用于在服务器上控制 redroid 里的明日方舟日常任务。

第一版能力：

- Telegram 白名单鉴权。
- 单设备串行任务队列。
- `/fight` 刷理智。
- `/daily` 日常组合任务：邮件、信用商店、公开招募、刷理智、基建换班、最后领取每日/周常任务奖励。
- `/screenshot` 即时截图，可在 MAA 任务执行中查看当前画面。
- `/stop` 停止当前 maa-cli 任务。
- `/status` 查看队列、ADB、maa-cli 状态。
- `/setstage` 设置 `/daily` 和无参数 `/fight` 使用的默认刷图关卡。
- `/setschedule` 设置定时 daily 的执行时间。

## 架构

```text
Telegram -> maa-bot(Python) -> maa-cli -> MaaCore -> ADB -> redroid
```

Bot 不直接调用 MaaCore SDK，而是为每次任务生成 maa-cli 自定义任务 JSON，然后执行 `maa run <task>`。这样第一版和 MAA 官方任务协议保持一致，后续扩展新任务只需要调整任务 JSON 生成逻辑。

参考文档：

- MAA 官方站点：https://maa.plus/
- maa-cli 安装：https://docs.maa.plus/zh-cn/manual/cli/install.html
- maa-cli 使用：https://docs.maa.plus/zh-cn/manual/cli/usage.html
- MAA 集成任务协议：https://docs.maa.plus/zh-cn/protocol/integration.html
- Linux 设备环境：https://docs.maa.plus/zh-cn/manual/device/linux.html

## 快速开始

本地开发使用 uv：

```bash
uv sync
```

准备 `.env`：

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
TELEGRAM_BOT_TOKEN=123456:replace_me
TELEGRAM_ALLOWED_USER_IDS=123456789
ARKNIGHTS_LOGIN_ENABLED=
ARKNIGHTS_LOGIN_USERNAME=
ARKNIGHTS_LOGIN_PASSWORD=
```

`TELEGRAM_ALLOWED_USER_IDS` 是允许使用 Bot 的 Telegram user id，多个用户用英文逗号分隔。
`ARKNIGHTS_LOGIN_USERNAME` 和 `ARKNIGHTS_LOGIN_PASSWORD` 只在启用自动登录时需要。
当前 `docker-compose.yml` 让 Bot 容器使用宿主机网络。**默认不使用代理**，可直连 Telegram 的环境（如美国服务器）无需任何额外配置。如果你所在网络无法直连 Telegram（如中国大陆），在 `.env` 中设置 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`（例如 `http://127.0.0.1:7890`）即可；宿主机上需有对应代理在运行。

如需调整 MAA 行为：

```bash
cp config/bot.example.toml config/bot.toml
```

然后编辑 `config/bot.toml`，例如理智药使用策略、公开招募、基建换班选项等。默认刷图关卡和定时 daily 时间部署后用 Telegram 的 `/setstage`、`/setschedule` 设置。

自定义基建排班按 maa-cli 官方任务配置格式生成。把基建计划 JSON 放到容器内 `$MAA_CONFIG_DIR/infrast`，在 `[base]` 设置 `mode = 10000` 和 `filename = "normal.json"`；需要按时间选择子计划时，可以写 `[[base.variants]]`，其中 `condition` 和 `params.plan_index` 会原样写入 `Infrast` 子任务。

启动：

```bash
docker compose up -d --build
```

首次部署后进入容器安装 MaaCore 和资源：

```bash
docker compose run --rm maa-bot maa install
```

安装完成后重启服务：

```bash
docker compose up -d
```

## redroid 前置条件

服务器必须能运行 redroid。通常需要宿主机内核支持 binder/ashmem 或 binderfs，并允许 privileged 容器。

启动后确认 ADB：

```bash
docker compose exec maa-bot adb connect 127.0.0.1:5555
docker compose exec maa-bot adb -s 127.0.0.1:5555 get-state
```

默认建议先人工完成明日方舟账号登录、验证码、安全验证和资源下载，可以用 scrcpy、VNC、投屏工具或 ADB 截图/输入完成首次登录。`redroid-data:/data` 会持久化登录态。

默认 maa-cli profile 使用 `touch_mode = "ADB"`。在 redroid 环境里这个模式比 `MaaTouch` 慢一些，但更稳定。

如果要尝试官服账号密码自动登录，在 `config/bot.toml` 中设置：

```toml
[login]
enabled = true
```

也可以在 `.env` 中设置 `ARKNIGHTS_LOGIN_ENABLED=true`。然后在 `.env` 中填写 `ARKNIGHTS_LOGIN_USERNAME` 和 `ARKNIGHTS_LOGIN_PASSWORD`。Bot 会先用 MAA 的 `StartUp` 单独启动游戏；启动失败会关闭游戏并按 `[maa].startup_retries` 重试。自动登录器只在后续 MAA 任务失败且当前界面明确是官服登录页时，才输入账号密码并重试一次。该自动登录器只适配当前 redroid 的 1280x720 官服登录页；遇到验证码、安全验证、错误密码或页面变化时会失败退出，不会尝试绕过验证。

如果游戏客户端强制更新导致 `StartUp` 失败，Bot 会在日志包含疑似客户端过期信号时，从官服下载 APK 并通过 `adb install -r` 安装，然后重试一次启动。该行为由 `[client_update]` 控制；安装成功后会删除 `$MAA_CONFIG_DIR/cache/arknights-official.apk`，安装失败时会保留该文件方便排查。

MaaCore 和 MAA 资源可静默自动更新。默认 compose 部署会在每次非截图 MAA 任务前按间隔执行 `maa update --batch --test-time 0 stable`，由 `[maa_update]` 或环境变量 `MAA_CORE_UPDATE_*` 控制；更新失败只写日志并继续执行当前任务，避免 GitHub 网络抖动阻塞日常。

## Bot 命令

```text
/status
/setstage [stage|clear]
/setschedule [HH:MM ...|clear]
/fight [stage] [medicine] [times]
/daily
/screenshot
/closegame
/stop
/help
```

`/screenshot` 不进入 MAA 任务队列，会直接通过 ADB 抓取当前 redroid 画面；因此 `/daily` 等长任务执行期间也可以随时发送 `/screenshot` 看现场。
`/closegame` 会先停止当前 maa-cli 任务，再通过 ADB 关闭明日方舟 App，不会停止 redroid 容器或清除登录态。
Telegram 只注册 `/status`、`/setstage`、`/setschedule`、`/fight`、`/daily`、`/screenshot`、`/closegame`、`/stop`、`/help`。

示例：

```text
/setstage 1-7
/setstage
/setschedule 08:00 20:00
/setschedule
/fight 1-7
/fight 1-7 2
/fight 1-7 2 10
```

`/daily` 和 `/fight` 直接操作 redroid 里当前登录的客户端。部署时需要确保 redroid 登录的是要操作的账号。`/daily` 会在刷理智、公招和基建处理后再领取每日/周常任务奖励，避免先领奖导致当天任务进度没结算进去。
`/daily` 完整成功后会自动关闭明日方舟 App；失败或手动 `/stop` 取消时会保留现场，方便继续 `/screenshot` 排查。

未传 `stage` 时使用 `/setstage` 保存的默认关卡，运行时状态在 `$MAA_CONFIG_DIR/bot-state.sqlite`。如果没有默认关卡，Bot 会拒绝 `/daily` 或无参数 `/fight` 排队，避免 MAA 在终端或活动总览页找不到目标关卡而长时间停住。`/fight CE-6` 只影响本次任务，不会覆盖默认关卡。

邮件领取通过 `[award].mail = true` 开启，会在 daily 前段执行。每日/周常任务奖励通过 `[award].award = true` 开启，会在 daily 最后执行。公开招募是单独的 `[recruit]` 配置，不是 `[award].recruit`；后者对应限定卡池每日免费单抽。默认公开招募策略会刷新低星标签，只确认 4 星及以上组合，并对确认的 4 星及以上招募使用加急许可。

定时 daily 的开关、时区和通知 chat 通过 `config/bot.toml` 配置；具体执行时间优先使用 `/setschedule` 保存在 SQLite 里的运行时值：

```toml
[schedule]
enabled = true
daily_times = ["08:00", "20:00"]
timezone = "Asia/Shanghai"
notify_chat_id = 123456789
```

`daily_times` 是旧部署或 SQLite 为空时的兜底值。定时任务会进入同一个任务队列；如果到点时已有任务，会排队等待。`notify_chat_id` 是接收排队通知和执行结果的 Telegram chat id。

## 运行测试

```bash
uv run python -m unittest discover -v
```

这些测试只覆盖 Bot 服务自身的配置、任务生成和队列逻辑，不需要 Telegram、redroid 或 maa-cli。

## 重要目录

- `maa_tg_bot/`：Bot 服务源码。
- `config/bot.example.toml`：Bot 配置模板。
- `config/maa-profile.toml`：maa-cli 默认 profile，会在容器启动时复制到 `/data/maa-config/profiles/default.toml`。
- `docker-compose.yml`：redroid 和 maa-bot 编排。
- `/root/.local/share/maa`：容器内 MaaCore 动态库和资源目录，由 `maa-data` 卷持久化。
- `/data/maa-config`：容器内 maa-cli 配置和生成的任务文件。
- `/data/logs`：Bot 和 maa-cli 执行日志。
