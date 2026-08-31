# -*- coding: utf-8 -*-
"""Validate that a decision signal's price levels form a tradeable plan.

一个方向性信号要能被当作交易来记分，必须同时满足三件事：

1. **完整性** —— 必须同时有入场、止损、目标。缺任何一个，这条信号无法执行，
   也就无法作为交易被评分。
2. **几何一致性** —— 止损与目标必须按方向正确地夹住入场价。看多必须
   ``stop < entry < target``，看空必须 ``target < entry < stop``。方向来自
   ``action``，绝不能从止损/目标的大小关系反推 —— 那样会把"levels 写反了"这个
   真实缺陷静默地当成正常方向。
3. **可达性** —— 目标必须在该信号自己的时间窗口内有可能被触及。用标的近期
   日均振幅衡量：如果需要连续多日"整段振幅都朝有利方向走"才能到目标，那么这
   个目标在该horizon下结构性不可达，信号永远停在"既没止损也没到目标"。

第 3 条来自 2026-08-31 的实测：15 条带完整价位的信号里有 10 条的目标在自身
horizon 内不可达（即便假设每天整段振幅都朝有利方向）。实测可捕获比例仅
``CAPTURE_FRACTION``，因此乐观估计还要再打折。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

BULLISH_ACTIONS = frozenset({"buy", "add"})
BEARISH_ACTIONS = frozenset({"sell", "reduce", "avoid"})
NON_DIRECTIONAL_ACTIONS = frozenset({"watch", "alert", "hold"})

# 实测：|open-close| / (high-low)，674 根日线，.NS 标的，2026-06-01 起。
# 没人能吃满整段振幅，用它把"整段振幅"折算成实际可捕获的幅度。
CAPTURE_FRACTION = 0.42

# 最低回报风险比。低于此值时，即使方向常常正确，长期期望仍为负。
MIN_REWARD_RISK = 1.2

# 目标可达性余量：需要的"有利振幅天数"不得超过 horizon 天数。
HORIZON_DAYS = {"intraday": 1, "1d": 1, "3d": 3, "5d": 5, "10d": 10}

LevelIssue = Literal[
    "missing_entry",
    "missing_stop",
    "missing_target",
    "non_positive_price",
    "levels_inconsistent",
    "reward_risk_too_low",
    "target_unreachable_in_horizon",
]


@dataclass(frozen=True)
class LevelValidation:
    """Outcome of validating one signal's levels."""

    ok: bool
    issues: tuple[LevelIssue, ...] = ()
    reward_risk: Optional[float] = None
    #: 需要多少个"整段日振幅全部朝有利方向"的交易日才能触及目标（乐观上界）
    favourable_days_to_target: Optional[float] = None
    #: 按实测可捕获比例折算后的天数（更接近现实）
    realistic_days_to_target: Optional[float] = None
    detail: dict = field(default_factory=dict)


def _direction(action: Optional[str]) -> Optional[bool]:
    """True=看多, False=看空, None=不表达方向。方向只来自 action。"""
    normalized = (action or "").strip().lower()
    if normalized in BULLISH_ACTIONS:
        return True
    if normalized in BEARISH_ACTIONS:
        return False
    return None


def _positive(value: Optional[float]) -> bool:
    return value is not None and isinstance(value, (int, float)) and value > 0


def validate_levels(
    *,
    action: Optional[str],
    entry: Optional[float],
    stop_loss: Optional[float],
    target_price: Optional[float],
    horizon: Optional[str] = None,
    average_daily_range_pct: Optional[float] = None,
    min_reward_risk: float = MIN_REWARD_RISK,
    capture_fraction: float = CAPTURE_FRACTION,
) -> LevelValidation:
    """Validate one signal's levels.

    ``average_daily_range_pct`` 为该标的近期日均 ``(high-low)/open*100``。
    省略时跳过可达性检查（其余检查照常执行）。
    """
    issues: list[LevelIssue] = []
    detail: dict = {}

    direction = _direction(action)
    if direction is None:
        # watch / alert 不表达方向，本来就不作为交易记分，不做几何校验。
        return LevelValidation(ok=True, issues=(), detail={"skipped": "non_directional"})

    # 入场价：看多必须有计划入场价；看空/减仓是对已有仓位的退出建议，
    # 参考价即当前价，因此 entry 可缺省 —— 但缺省时无法做几何与可达性校验，
    # 这一点必须显式记录，不能当作"通过"。
    entry_required = direction is True
    if not _positive(entry):
        if entry_required:
            issues.append("missing_entry" if entry is None else "non_positive_price")
        else:
            detail["entry"] = "absent — exit advice scored from a reference price"
    if not _positive(stop_loss):
        issues.append("missing_stop" if stop_loss is None else "non_positive_price")
    if not _positive(target_price):
        issues.append("missing_target" if target_price in (None, 0) else "non_positive_price")

    if issues:
        return LevelValidation(ok=False, issues=tuple(issues), detail=detail)

    if not _positive(entry):
        # 退出建议且无参考价：完整性检查已通过，但几何/可达性无法判定。
        detail["skipped"] = "geometry_and_reachability_need_a_reference_price"
        return LevelValidation(ok=True, issues=(), detail=detail)

    entry_f = float(entry)
    stop_f = float(stop_loss)
    target_f = float(target_price)

    bracketed = (
        stop_f < entry_f < target_f if direction else target_f < entry_f < stop_f
    )
    if not bracketed:
        issues.append("levels_inconsistent")
        detail["expected"] = (
            "stop < entry < target" if direction else "target < entry < stop"
        )
        # 几何不成立时，风险/回报没有意义，直接返回。
        return LevelValidation(ok=False, issues=tuple(issues), detail=detail)

    risk = abs(entry_f - stop_f)
    reward = abs(target_f - entry_f)
    reward_risk = reward / risk if risk > 0 else None
    if reward_risk is None or reward_risk < min_reward_risk:
        issues.append("reward_risk_too_low")

    favourable_days = realistic_days = None
    if average_daily_range_pct and average_daily_range_pct > 0:
        target_distance_pct = reward / entry_f * 100
        favourable_days = target_distance_pct / average_daily_range_pct
        realistic_days = (
            favourable_days / capture_fraction if capture_fraction > 0 else None
        )
        allowed = HORIZON_DAYS.get((horizon or "intraday").strip().lower(), 1)
        detail["horizon_days"] = allowed
        detail["target_distance_pct"] = round(target_distance_pct, 4)
        if favourable_days > allowed:
            issues.append("target_unreachable_in_horizon")

    return LevelValidation(
        ok=not issues,
        issues=tuple(issues),
        reward_risk=reward_risk,
        favourable_days_to_target=favourable_days,
        realistic_days_to_target=realistic_days,
        detail=detail,
    )
