from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field


TASK_LABELS = {
    "Start game": "启动游戏",
    "启动游戏": "启动游戏",
    "Collect awards": "领取奖励",
    "领取奖励": "领取奖励",
    "领取邮件": "领取邮件",
    "领取任务奖励": "领取任务奖励",
    "Credit store": "信用商店",
    "信用商店": "信用商店",
    "Recruit": "公开招募",
    "公开招募": "公开招募",
    "Fight": "刷理智",
    "刷理智": "刷理智",
    "Base shift": "基建换班",
    "基建换班": "基建换班",
    "Close game": "关闭游戏",
    "关闭游戏": "关闭游戏",
}

STATUS_LABELS = {
    "Completed": "完成",
    "Error": "失败",
    "Failed": "失败",
    "Cancelled": "已取消",
    "Skipped": "已跳过",
}

FACILITY_LABELS = {
    "Mfg": "制造站",
    "Trade": "贸易站",
    "Reception": "会客室",
    "Control": "控制中枢",
    "Power": "发电站",
    "Office": "办公室",
    "Dorm": "宿舍",
}

FACILITY_ORDER = ("Mfg", "Trade", "Reception", "Control", "Power", "Office", "Dorm")

PRODUCT_LABELS = {
    "CombatRecord": "作战记录",
    "PureGold": "赤金",
    "Money": "龙门币",
    "SyntheticJade": "合成玉",
    "OriginStone": "源石碎片",
    "Chip": "芯片",
    "Drone": "无人机",
    "HR": "公开招募",
    "General": "通用",
    "MoodAddition": "心情恢复",
}

SUMMARY_HEADER_RE = re.compile(
    r"^\[(?P<name>.+?)\]\s+"
    r"(?P<start>\S+)\s+-\s+(?P<end>\S+)\s+"
    r"\((?P<duration>[^)]*)\)\s+(?P<status>\S+)"
)
FIGHT_RE = re.compile(r"^Fight\s+(?P<stage>\S+)\s+(?P<times>\d+)\s+times")
DROPS_RE = re.compile(r"^total drops:\s*(?P<drops>.+)$", re.IGNORECASE)
FACILITY_RE = re.compile(
    r"^(?P<facility>[A-Za-z]+)(?:\((?P<product>[^)]+)\))?\s+with operators:"
)


@dataclass(frozen=True)
class MaaSummaryItem:
    name: str
    duration: str
    status: str
    details: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return TASK_LABELS.get(self.name, self.name)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


def build_daily_completion_details(output: str) -> str:
    items = parse_maa_summary(output)
    if not items:
        return ""

    lines = ["完成明细："]
    for item in items:
        lines.append(f"- {format_summary_item(item)}")
    return "\n".join(lines)


def parse_maa_summary(output: str) -> list[MaaSummaryItem]:
    summary_lines = _summary_lines(output)
    if not summary_lines:
        return []

    items: list[MaaSummaryItem] = []
    current: MaaSummaryItem | None = None
    details: list[str] = []
    for raw_line in summary_lines:
        line = raw_line.strip()
        if not line or set(line) == {"-"}:
            continue
        match = SUMMARY_HEADER_RE.match(line)
        if match:
            if current is not None:
                items.append(
                    MaaSummaryItem(
                        name=current.name,
                        duration=current.duration,
                        status=current.status,
                        details=details,
                    )
                )
            current = MaaSummaryItem(
                name=match.group("name"),
                duration=match.group("duration"),
                status=match.group("status"),
            )
            details = []
            continue
        if current is not None:
            details.append(line)

    if current is not None:
        items.append(
            MaaSummaryItem(
                name=current.name,
                duration=current.duration,
                status=current.status,
                details=details,
            )
        )
    return items


def format_summary_item(item: MaaSummaryItem) -> str:
    if item.label == "刷理智":
        fight_detail = _fight_detail(item.details)
        if fight_detail:
            return f"{item.label}：{fight_detail}"
    if item.label == "基建换班":
        base_detail = _base_detail(item.details)
        if base_detail:
            return f"{item.label}：{base_detail}"
    return f"{item.label}：{item.status_label}，用时 {_localize_duration(item.duration)}"


def _summary_lines(output: str) -> list[str]:
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "Summary":
            return lines[index + 1 :]
    return []


def _fight_detail(lines: list[str]) -> str:
    stage = ""
    times = ""
    drops = ""
    for line in lines:
        fight = FIGHT_RE.match(line)
        if fight:
            stage = fight.group("stage")
            times = fight.group("times")
            continue
        drops_match = DROPS_RE.match(line)
        if drops_match:
            drops = drops_match.group("drops")

    parts: list[str] = []
    if stage and times:
        parts.append(f"{stage} × {times}")
    if drops:
        parts.append(f"掉落：{drops}")
    return "，".join(parts)


def _base_detail(lines: list[str]) -> str:
    facility_counts: Counter[str] = Counter()
    product_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for line in lines:
        match = FACILITY_RE.match(line)
        if not match:
            continue
        facility = match.group("facility")
        product = match.group("product")
        facility_counts[facility] += 1
        if product:
            product_counts[facility][product] += 1

    if not facility_counts:
        return ""

    parts: list[str] = []
    ordered_facilities = [
        *[facility for facility in FACILITY_ORDER if facility in facility_counts],
        *[facility for facility in facility_counts if facility not in FACILITY_ORDER],
    ]
    for facility in ordered_facilities:
        label = FACILITY_LABELS.get(facility, facility)
        text = f"{label} {facility_counts[facility]} 个"
        products = product_counts.get(facility)
        if products:
            product_text = "、".join(
                f"{PRODUCT_LABELS.get(product, product)} {count}"
                for product, count in products.items()
            )
            text += f"（{product_text}）"
        parts.append(text)
    return "，".join(parts) + "，已执行换班"


def _localize_duration(duration: str) -> str:
    return re.sub(
        r"(\d+)\s*([hms])",
        lambda match: f"{match.group(1)}{_duration_unit(match.group(2))}",
        duration,
    )


def _duration_unit(unit: str) -> str:
    return {"h": "小时", "m": "分", "s": "秒"}[unit]
