"""Hidden ground truth: the variables nobody measures, written per minute per team.

The schedule is generated before kick-off (planned press and risk changes, sub
minutes). The simulator realises it minute by minute, adds reactive changes
(score state, substitutions) and momentum, and logs the realised values. The log
never enters the event stream; it goes to the Lakehouse as training and scoring
data for a hidden-state estimator.
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field

MINUTES = 101


@dataclass
class TeamSchedule:
    team_id: str
    press_intent: list[float]
    risk_appetite: list[float]
    planned_changes: list[dict]
    sub_minutes: list[int]


@dataclass
class TruthLog:
    match_id: str
    seed: int
    schedules: dict[str, TeamSchedule]
    minutes: list[dict] = field(default_factory=list)
    instruction_changes: list[dict] = field(default_factory=list)
    policies: dict[str, dict] = field(default_factory=dict)

    def log_minute(self, minute: int, team_id: str, momentum: float, fatigue: float, press_intent: float, risk_appetite: float) -> None:
        self.minutes.append({
            "minute": minute, "team_id": team_id, "momentum": round(momentum, 3), "fatigue": round(fatigue, 3),
            "press_intent": round(press_intent, 3), "risk_appetite": round(risk_appetite, 3),
        })

    def log_change(self, minute: int, team_id: str, cause: str, variable: str, delta: float, detail: str = "") -> None:
        self.instruction_changes.append({
            "minute": minute, "team_id": team_id, "cause": cause, "variable": variable,
            "delta": round(delta, 3), "detail": detail,
        })

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=1)


def make_schedule(team_id: str, press_height: float, direct: float, rng: random.Random) -> TeamSchedule:
    base_press = min(0.95, max(0.1, press_height + rng.gauss(0, 0.05)))
    base_risk = min(0.95, max(0.1, 0.3 + 0.45 * direct + rng.gauss(0, 0.05)))
    press = [base_press] * MINUTES
    risk = [base_risk] * MINUTES
    planned: list[dict] = []
    # A planned tactical shift in roughly half the matches: a press step up or down after the hour.
    if rng.random() < 0.5:
        minute = rng.randint(48, 76)
        delta = rng.choice([-0.22, -0.15, 0.15, 0.22])
        for m in range(minute, MINUTES):
            press[m] = min(0.95, max(0.05, press[m] + delta))
        planned.append({"minute": minute, "variable": "press_intent", "delta": delta, "cause": "plan"})
    # Risk drifts up slowly in the last quarter for direct teams.
    for m in range(70, MINUTES):
        risk[m] = min(0.95, risk[m] + 0.004 * (m - 70) * direct)
    subs = sorted(rng.sample(range(56, 84), 3))
    return TeamSchedule(team_id=team_id, press_intent=[round(v, 3) for v in press],
                        risk_appetite=[round(v, 3) for v in risk], planned_changes=planned, sub_minutes=subs)
