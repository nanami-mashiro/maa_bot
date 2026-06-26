import unittest

from maa_tg_bot.maa_summary import build_daily_completion_details, parse_maa_summary


DAILY_SUMMARY = """\
Summary
----------------------------------------
[Start game] 01:26:14 - 01:27:09 (55s) Completed
----------------------------------------
[领取邮件] 01:27:10 - 01:27:15 (5s) Completed
----------------------------------------
[Credit store] 01:27:15 - 01:29:30 (2m 14s) Completed
----------------------------------------
[Recruit] 01:29:30 - 01:29:45 (15s) Completed
----------------------------------------
[Fight] 01:29:30 - 01:33:18 (3m 47s) Completed
Fight MT-10 6 times, drops:
1. 化合切削液 × 3, 蒙恩的奶嘴 × 126, 龙门币 × 1512
total drops: 化合切削液 × 3, 蒙恩的奶嘴 × 126, 龙门币 × 1512
----------------------------------------
[Base shift] 01:33:19 - 01:42:23 (9m 4s) Completed
Mfg(CombatRecord) with operators: unknown
Mfg(PureGold) with operators: unknown
Mfg(PureGold) with operators: unknown
Mfg(CombatRecord) with operators: unknown
Trade(Money) with operators: unknown
Trade(Money) with operators: unknown
----------------------------------------
[领取任务奖励] 01:42:24 - 01:42:35 (11s) Completed
"""


class MaaSummaryTests(unittest.TestCase):
    def test_parse_maa_summary_items(self):
        items = parse_maa_summary(DAILY_SUMMARY)

        self.assertEqual(
            [item.label for item in items],
            ["启动游戏", "领取邮件", "信用商店", "公开招募", "刷理智", "基建换班", "领取任务奖励"],
        )
        self.assertEqual(items[0].status_label, "完成")
        self.assertEqual(items[4].details[0], "Fight MT-10 6 times, drops:")

    def test_build_daily_completion_details_from_english_summary(self):
        details = build_daily_completion_details(DAILY_SUMMARY)

        self.assertIn("完成明细：", details)
        self.assertIn("启动游戏：完成，用时 55秒", details)
        self.assertIn("领取邮件：完成，用时 5秒", details)
        self.assertIn("信用商店：完成，用时 2分 14秒", details)
        self.assertIn("公开招募：完成，用时 15秒", details)
        self.assertIn("刷理智：MT-10 × 6，掉落：化合切削液 × 3", details)
        self.assertIn("制造站 4 个（作战记录 2、赤金 2）", details)
        self.assertIn("贸易站 2 个（龙门币 2）", details)
        self.assertIn("领取任务奖励：完成，用时 11秒", details)

    def test_build_daily_completion_details_from_chinese_summary(self):
        details = build_daily_completion_details(
            """\
Summary
----------------------------------------
[启动游戏] 08:00:00 - 08:00:03 (3s) Completed
----------------------------------------
[刷理智] 08:00:03 - 08:01:00 (57s) Completed
Fight 1-7 2 times, drops:
total drops: 源岩 × 4
"""
        )

        self.assertIn("启动游戏：完成，用时 3秒", details)
        self.assertIn("刷理智：1-7 × 2，掉落：源岩 × 4", details)

    def test_missing_summary_returns_empty_details(self):
        self.assertEqual(build_daily_completion_details("Fight MT-10 6 times"), "")


if __name__ == "__main__":
    unittest.main()
