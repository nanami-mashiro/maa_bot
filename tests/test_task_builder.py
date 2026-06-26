import tempfile
import unittest
from pathlib import Path

from maa_tg_bot.config import load_config
from maa_tg_bot.models import TaskKind
from maa_tg_bot.task_builder import MaaTaskBuilder


def test_config():
    return load_config(
        path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_USER_IDS": "123",
            "MAA_AUTO_STARTUP": "true",
            "MAA_AUTO_CLOSEDOWN": "true",
            "MAA_DEFAULT_STAGE": "1-7",
        },
    )


class TaskBuilderTests(unittest.TestCase):
    def test_fight_task_uses_overrides_and_wrappers(self):
        builder = MaaTaskBuilder(test_config())
        doc = builder.build(TaskKind.FIGHT, {"stage": "CE-6", "medicine": 2, "times": 10})

        types = [task["type"] for task in doc["tasks"]]
        self.assertEqual(types, ["StartUp", "Fight", "CloseDown"])
        names = [task["name"] for task in doc["tasks"]]
        self.assertEqual(names, ["启动游戏", "刷理智", "关闭游戏"])
        fight = doc["tasks"][1]
        self.assertEqual(fight["params"]["stage"], "CE-6")
        self.assertEqual(fight["params"]["medicine"], 2)
        self.assertEqual(fight["params"]["times"], 10)

    def test_daily_contains_daily_task_set(self):
        builder = MaaTaskBuilder(test_config())
        doc = builder.build(TaskKind.DAILY)

        types = [task["type"] for task in doc["tasks"]]
        self.assertEqual(
            types,
            ["StartUp", "Award", "Mall", "Recruit", "Fight", "Infrast", "Award", "CloseDown"],
        )
        names = [task["name"] for task in doc["tasks"]]
        self.assertEqual(
            names,
            [
                "启动游戏",
                "领取邮件",
                "信用商店",
                "公开招募",
                "刷理智",
                "基建换班",
                "领取任务奖励",
                "关闭游戏",
            ],
        )
        mail_award = doc["tasks"][1]
        recruit = doc["tasks"][3]
        final_award = doc["tasks"][6]
        self.assertEqual(
            mail_award["params"],
            {
                "enable": True,
                "award": False,
                "mail": True,
                "recruit": False,
                "orundum": False,
                "mining": False,
                "specialaccess": False,
            },
        )
        self.assertEqual(
            final_award["params"],
            {
                "enable": True,
                "award": True,
                "mail": False,
                "recruit": False,
                "orundum": False,
                "mining": False,
                "specialaccess": False,
            },
        )
        self.assertEqual(
            recruit["params"],
            {
                "enable": True,
                "refresh": True,
                "select": [5, 4],
                "confirm": [5, 4],
                "times": 4,
                "set_time": True,
                "expedite": True,
                "skip_robot": True,
                "extra_tags_mode": 0,
                "server": "CN",
            },
        )
        self.assertEqual(types[-3], "Infrast")

    def test_daily_omits_disabled_recruit_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "token"
allowed_user_ids = [123]

[recruit]
enabled = false
""",
                encoding="utf-8",
            )
            builder = MaaTaskBuilder(load_config(path=path, env={}))

        doc = builder.build(TaskKind.DAILY)

        self.assertNotIn("Recruit", [task["type"] for task in doc["tasks"]])

    def test_daily_can_omit_disabled_award_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.toml"
            path.write_text(
                """
[telegram]
token = "token"
allowed_user_ids = [123]

[award]
award = false
mail = false
""",
                encoding="utf-8",
            )
            builder = MaaTaskBuilder(load_config(path=path, env={}))

        doc = builder.build(TaskKind.DAILY)

        self.assertNotIn("Award", [task["type"] for task in doc["tasks"]])

    def test_startup_uses_account_name_override(self):
        builder = MaaTaskBuilder(test_config())
        doc = builder.build(TaskKind.DAILY, {"account_name": "doctor-a"})

        startup = doc["tasks"][0]
        self.assertEqual(startup["type"], "StartUp")
        self.assertEqual(startup["params"]["account_name"], "doctor-a")

    def test_can_build_startup_task_only(self):
        builder = MaaTaskBuilder(test_config())
        doc = builder.build_startup({"account_name": "doctor-a"})

        self.assertEqual([task["type"] for task in doc["tasks"]], ["StartUp"])
        self.assertEqual(doc["tasks"][0]["params"]["account_name"], "doctor-a")

    def test_can_build_main_task_without_startup(self):
        builder = MaaTaskBuilder(test_config())
        doc = builder.build(TaskKind.FIGHT, {"stage": "CE-6"}, include_startup=False)

        self.assertEqual([task["type"] for task in doc["tasks"]], ["Fight", "CloseDown"])

    def test_base_task_includes_custom_plan_variants(self):
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

[[base.variants]]
condition = { type = "Time", end = "12:00:00" }
params = { plan_index = 1 }
""",
                encoding="utf-8",
            )
            builder = MaaTaskBuilder(load_config(path=path, env={}))

        doc = builder.build(TaskKind.DAILY)
        base = next(task for task in doc["tasks"] if task["type"] == "Infrast")

        self.assertEqual(base["type"], "Infrast")
        self.assertEqual(base["params"]["mode"], 10000)
        self.assertEqual(base["params"]["filename"], "normal.json")
        self.assertEqual(base["variants"][0]["condition"], {"type": "Time", "end": "12:00:00"})
        self.assertEqual(base["variants"][0]["params"], {"plan_index": 1})


if __name__ == "__main__":
    unittest.main()
