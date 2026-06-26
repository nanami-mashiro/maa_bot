from __future__ import annotations

from typing import Any

from .config import AppConfig
from .models import TaskKind


class MaaTaskBuilder:
    def __init__(self, config: AppConfig):
        self.config = config

    def build(
        self,
        kind: TaskKind,
        options: dict[str, Any] | None = None,
        *,
        include_startup: bool | None = None,
    ) -> dict[str, Any]:
        options = options or {}
        tasks = self._task_sequence(kind, options, include_startup)
        return {"tasks": tasks}

    def build_startup(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"tasks": [self._startup_task(options or {})]}

    def _task_sequence(
        self,
        kind: TaskKind,
        options: dict[str, Any],
        include_startup: bool | None,
    ) -> list[dict[str, Any]]:
        if kind == TaskKind.DAILY:
            inner = []
            early_award_task = self._early_award_task()
            if early_award_task is not None:
                inner.append(early_award_task)
            inner.append(self._mall_task())
            if self.config.recruit.enabled:
                inner.append(self._recruit_task())
            inner.extend([self._fight_task(options), self._base_task()])
            final_award_task = self._final_award_task()
            if final_award_task is not None:
                inner.append(final_award_task)
        elif kind == TaskKind.FIGHT:
            inner = [self._fight_task(options)]
        else:
            raise ValueError(f"不支持的 MAA 自定义任务类型：{kind}")

        should_include_startup = (
            include_startup if include_startup is not None else self.config.maa.auto_startup
        )
        if should_include_startup:
            inner = [self._startup_task(options)] + inner
        if self.config.maa.auto_closedown:
            inner = inner + [self._closedown_task()]
        return inner

    def _startup_task(self, options: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "client_type": self.config.maa.client_type,
            "start_game_enabled": True,
        }
        account_name = str(options.get("account_name", self.config.maa.account_name)).strip()
        if account_name:
            params["account_name"] = account_name
        return {"name": "启动游戏", "type": "StartUp", "params": params}

    def _closedown_task(self) -> dict[str, Any]:
        return {
            "name": "关闭游戏",
            "type": "CloseDown",
            "params": {"client_type": self.config.maa.client_type},
        }

    def _fight_task(self, options: dict[str, Any]) -> dict[str, Any]:
        fight = self.config.fight
        params: dict[str, Any] = {
            "enable": True,
            "medicine": int(options.get("medicine", fight.medicine)),
            "expiring_medicine": int(options.get("expiring_medicine", fight.expiring_medicine)),
            "stone": int(options.get("stone", fight.stone)),
            "server": self.config.maa.server,
            "client_type": self.config.maa.client_type,
            "DrGrandet": bool(options.get("dr_grandet", fight.dr_grandet)),
        }

        stage = str(options.get("stage", fight.stage)).strip()
        if stage:
            params["stage"] = stage
        times = options.get("times", fight.times)
        if times is not None:
            params["times"] = int(times)
        series = options.get("series", fight.series)
        if series is not None:
            params["series"] = int(series)

        return {"name": "刷理智", "type": "Fight", "params": params}

    def _base_task(self) -> dict[str, Any]:
        base = self.config.base
        params: dict[str, Any] = {
            "enable": True,
            "mode": base.mode,
            "facility": base.facility,
            "drones": base.drones,
            "threshold": base.threshold,
            "replenish": base.replenish,
            "dorm_notstationed_enabled": base.dorm_notstationed_enabled,
            "dorm_trust_enabled": base.dorm_trust_enabled,
            "reception_message_board": base.reception_message_board,
            "reception_clue_exchange": base.reception_clue_exchange,
            "reception_send_clue": base.reception_send_clue,
        }
        if base.filename:
            params["filename"] = base.filename
        if base.plan_index is not None:
            params["plan_index"] = base.plan_index

        task: dict[str, Any] = {
            "name": "基建换班",
            "type": "Infrast",
            "params": params,
        }
        if base.variants:
            task["variants"] = base.variants
        return task

    def _mall_task(self) -> dict[str, Any]:
        mall = self.config.mall
        return {
            "name": "信用商店",
            "type": "Mall",
            "params": {
                "enable": True,
                "visit_friends": mall.visit_friends,
                "shopping": mall.shopping,
                "buy_first": mall.buy_first,
                "blacklist": mall.blacklist,
                "force_shopping_if_credit_full": mall.force_shopping_if_credit_full,
                "only_buy_discount": mall.only_buy_discount,
                "reserve_max_credit": mall.reserve_max_credit,
                "credit_fight": mall.credit_fight,
                "formation_index": mall.formation_index,
            },
        }

    def _recruit_task(self) -> dict[str, Any]:
        recruit = self.config.recruit
        task = {
            "name": "公开招募",
            "type": "Recruit",
            "params": {
                "enable": True,
                "refresh": recruit.refresh,
                "select": recruit.select,
                "confirm": recruit.confirm,
                "times": recruit.times,
                "set_time": recruit.set_time,
                "expedite": recruit.expedite,
                "skip_robot": recruit.skip_robot,
                "extra_tags_mode": recruit.extra_tags_mode,
                "server": self.config.maa.server,
            },
        }
        if recruit.expedite_times is not None:
            task["params"]["expedite_times"] = recruit.expedite_times
        return task

    def _early_award_task(self) -> dict[str, Any] | None:
        award = self.config.award
        if not any([award.mail, award.recruit, award.orundum, award.mining, award.specialaccess]):
            return None
        return self._award_task(
            "领取邮件",
            award=False,
            mail=award.mail,
            recruit=award.recruit,
            orundum=award.orundum,
            mining=award.mining,
            specialaccess=award.specialaccess,
        )

    def _final_award_task(self) -> dict[str, Any] | None:
        if not self.config.award.award:
            return None
        return self._award_task(
            "领取任务奖励",
            award=True,
            mail=False,
            recruit=False,
            orundum=False,
            mining=False,
            specialaccess=False,
        )

    def _award_task(
        self,
        name: str,
        *,
        award: bool,
        mail: bool,
        recruit: bool,
        orundum: bool,
        mining: bool,
        specialaccess: bool,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "type": "Award",
            "params": {
                "enable": True,
                "award": award,
                "mail": mail,
                "recruit": recruit,
                "orundum": orundum,
                "mining": mining,
                "specialaccess": specialaccess,
            },
        }
