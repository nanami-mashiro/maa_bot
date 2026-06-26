import tempfile
import unittest
from pathlib import Path

from maa_tg_bot.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_loads_required_config_from_env(self):
        config = load_config(
            path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
            env={
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_ALLOWED_USER_IDS": "123,456",
                "MAA_DEFAULT_STAGE": "1-7",
                "MAA_MEDICINE": "2",
            },
        )

        self.assertEqual(config.telegram.token, "token")
        self.assertEqual(config.telegram.allowed_user_ids, {123, 456})
        self.assertEqual(config.fight.stage, "1-7")
        self.assertEqual(config.fight.medicine, 2)
        self.assertEqual(config.maa.startup_retries, 3)
        self.assertTrue(config.client_update.enabled)
        self.assertEqual(
            config.client_update.download_url,
            "https://ak.hypergryph.com/downloads/android_lastest",
        )
        self.assertTrue(config.award.mail)
        self.assertFalse(config.award.recruit)
        self.assertTrue(config.recruit.enabled)
        self.assertTrue(config.recruit.refresh)
        self.assertEqual(config.recruit.select, [5, 4])
        self.assertEqual(config.recruit.confirm, [5, 4])
        self.assertEqual(config.recruit.times, 4)
        self.assertTrue(config.recruit.set_time)
        self.assertTrue(config.recruit.expedite)
        self.assertIsNone(config.recruit.expedite_times)
        self.assertTrue(config.recruit.skip_robot)
        self.assertEqual(config.recruit.extra_tags_mode, 0)
        self.assertFalse(config.login.enabled)

    def test_env_overrides_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "file-token"
allowed_user_ids = [111]

[fight]
stage = "CE-6"
""",
                encoding="utf-8",
            )

            config = load_config(
                path=path,
                env={
                    "TELEGRAM_BOT_TOKEN": "env-token",
                    "TELEGRAM_ALLOWED_USER_IDS": "222",
                },
            )

        self.assertEqual(config.telegram.token, "env-token")
        self.assertEqual(config.telegram.allowed_user_ids, {222})
        self.assertEqual(config.fight.stage, "CE-6")

    def test_requires_token_and_allowed_users(self):
        with self.assertRaises(ConfigError):
            load_config(path=Path("/tmp/nonexistent-maa-tg-bot.toml"), env={})

    def test_login_requires_credentials_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "token"
allowed_user_ids = [123]

[login]
enabled = true
""",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(path=path, env={})

    def test_login_loads_credentials_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "token"
allowed_user_ids = [123]

[login]
enabled = true
login_timeout_seconds = 34
""",
                encoding="utf-8",
            )

            config = load_config(
                path=path,
                env={
                    "ARKNIGHTS_LOGIN_USERNAME": "doctor@example.com",
                    "ARKNIGHTS_LOGIN_PASSWORD": "password123",
                },
            )

        self.assertTrue(config.login.enabled)
        self.assertEqual(config.login.username, "doctor@example.com")
        self.assertEqual(config.login.password, "password123")
        self.assertEqual(config.login.login_timeout_seconds, 34)

    def test_loads_base_custom_plan_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "token"
allowed_user_ids = [123]

[base]
mode = 10000
filename = "normal.json"
plan_index = 1

[[base.variants]]
condition = { type = "Time", start = "18:00:00", end = "04:00:00" }
params = { plan_index = 0 }
""",
                encoding="utf-8",
            )

            config = load_config(path=path, env={})

        self.assertEqual(config.base.mode, 10000)
        self.assertEqual(config.base.filename, "normal.json")
        self.assertEqual(config.base.plan_index, 1)
        self.assertEqual(config.base.variants[0]["params"], {"plan_index": 0})

    def test_loads_schedule_config_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "token"
allowed_user_ids = [123]

[schedule]
enabled = false
daily_times = ["7:5"]
timezone = "UTC"
notify_chat_id = 111
""",
                encoding="utf-8",
            )

            config = load_config(
                path=path,
                env={
                    "SCHEDULE_ENABLED": "true",
                    "SCHEDULE_DAILY_TIMES": "08:00,20:00",
                    "SCHEDULE_TIMEZONE": "Asia/Shanghai",
                    "SCHEDULE_NOTIFY_CHAT_ID": "456",
                },
            )

        self.assertTrue(config.schedule.enabled)
        self.assertEqual(config.schedule.daily_times, ["08:00", "20:00"])
        self.assertEqual(config.schedule.timezone, "Asia/Shanghai")
        self.assertEqual(config.schedule.notify_chat_id, 456)

    def test_rejects_invalid_schedule_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "token"
allowed_user_ids = [123]

[schedule]
daily_times = ["25:00"]
""",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(path=path, env={})

    def test_loads_startup_retries_from_env(self):
        config = load_config(
            path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
            env={
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_ALLOWED_USER_IDS": "123",
                "MAA_STARTUP_RETRIES": "5",
            },
        )

        self.assertEqual(config.maa.startup_retries, 5)

    def test_loads_client_update_config_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "token"
allowed_user_ids = [123]

[client_update]
enabled = false
download_url = "https://example.invalid/file.apk"
download_timeout_seconds = 10
install_timeout_seconds = 20
""",
                encoding="utf-8",
            )

            config = load_config(
                path=path,
                env={
                    "CLIENT_UPDATE_ENABLED": "true",
                    "CLIENT_UPDATE_DOWNLOAD_TIMEOUT_SECONDS": "30",
                },
            )

        self.assertTrue(config.client_update.enabled)
        self.assertEqual(config.client_update.download_url, "https://example.invalid/file.apk")
        self.assertEqual(config.client_update.download_timeout_seconds, 30)
        self.assertEqual(config.client_update.install_timeout_seconds, 20)

    def test_maa_core_update_default_interval_is_six_hours(self):
        config = load_config(
            path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
            env={
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_ALLOWED_USER_IDS": "123",
            },
        )

        self.assertEqual(config.maa_update.interval_seconds, 21600)

    def test_loads_maa_core_update_config_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "token"
allowed_user_ids = [123]

[maa_update]
enabled = false
channel = "beta"
interval_seconds = 600
timeout_seconds = 700
test_time = 2
""",
                encoding="utf-8",
            )

            config = load_config(
                path=path,
                env={
                    "MAA_CORE_UPDATE_ENABLED": "true",
                    "MAA_CORE_UPDATE_CHANNEL": "stable",
                    "MAA_CORE_UPDATE_INTERVAL_SECONDS": "3600",
                    "MAA_CORE_UPDATE_TIMEOUT_SECONDS": "1200",
                    "MAA_CORE_UPDATE_TEST_TIME": "0",
                },
            )

        self.assertTrue(config.maa_update.enabled)
        self.assertEqual(config.maa_update.channel, "stable")
        self.assertEqual(config.maa_update.interval_seconds, 3600)
        self.assertEqual(config.maa_update.timeout_seconds, 1200)
        self.assertEqual(config.maa_update.test_time, 0)

    def test_loads_recruit_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "token"
allowed_user_ids = [123]

[award]
mail = false

[recruit]
enabled = false
refresh = false
select = [6, 5]
confirm = [6]
times = 2
set_time = false
expedite = true
expedite_times = 1
skip_robot = false
extra_tags_mode = 1
""",
                encoding="utf-8",
            )

            config = load_config(path=path, env={})

        self.assertFalse(config.award.mail)
        self.assertFalse(config.recruit.enabled)
        self.assertFalse(config.recruit.refresh)
        self.assertEqual(config.recruit.select, [6, 5])
        self.assertEqual(config.recruit.confirm, [6])
        self.assertEqual(config.recruit.times, 2)
        self.assertFalse(config.recruit.set_time)
        self.assertTrue(config.recruit.expedite)
        self.assertEqual(config.recruit.expedite_times, 1)
        self.assertFalse(config.recruit.skip_robot)
        self.assertEqual(config.recruit.extra_tags_mode, 1)


if __name__ == "__main__":
    unittest.main()
