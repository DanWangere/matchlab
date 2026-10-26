"""A threat grid learned from the league itself, the same 12 x 8 cells as the Eventhouse
`ThreatGrid` function, and a rollout value built on it.

Why not raw goals: a 20-event rollout from midfield ends in a goal a few percent of the time,
so the difference between candidate passes drowns in sampling noise. Threat credits a branch
for every metre of danger it creates, which separates options after far fewer rollouts.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable, Optional

from .schema import Event

COLS, ROWS = 12, 8
CELL_X, CELL_Y = 105.0 / COLS, 68.0 / ROWS


def cell(x: float, y: float) -> tuple[int, int]:
    return max(0, min(int(x / CELL_X), COLS - 1)), max(0, min(int(y / CELL_Y), ROWS - 1))


class ThreatGrid:
    """p_goal[cx][cy]: the share of possessions that moved the ball into the cell and ended in a goal."""

    def __init__(self, p_goal: Optional[list[list[float]]] = None, p_shot: Optional[list[list[float]]] = None, n: Optional[list[list[int]]] = None):
        self.p_goal = p_goal or [[0.0] * ROWS for _ in range(COLS)]
        self.p_shot = p_shot or [[0.0] * ROWS for _ in range(COLS)]
        self.n = n or [[0] * ROWS for _ in range(COLS)]

    @classmethod
    def learn(cls, matches: Iterable[list[Event]]) -> "ThreatGrid":
        hits: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0, 0])  # n, shots, goals
        for events in matches:
            by_poss: dict[str, dict[str, Any]] = defaultdict(lambda: {"cells": [], "shot": False, "goal": False})
            for e in events:
                if not e.possession_id:
                    continue
                p = by_poss[e.possession_id]
                if e.type in ("pass", "carry") and e.end_x is not None:
                    p["cells"].append(cell(e.end_x, e.end_y or 0.0))
                elif e.type == "shot":
                    p["shot"] = True
                    if e.outcome == "goal":
                        p["goal"] = True
            for p in by_poss.values():
                for c in p["cells"]:
                    h = hits[c]
                    h[0] += 1
                    h[1] += 1 if p["shot"] else 0
                    h[2] += 1 if p["goal"] else 0
        g = cls()
        for (cx, cy), (n, s, go) in hits.items():
            g.n[cx][cy] = n
            g.p_shot[cx][cy] = round(s / n, 4)
            g.p_goal[cx][cy] = round(go / n, 4)
        # Smooth empty or thin cells with the row mean so a sparse league does not leave holes.
        for cy in range(ROWS):
            vals = [(g.p_goal[cx][cy], g.n[cx][cy]) for cx in range(COLS) if g.n[cx][cy] >= 20]
            if not vals:
                continue
            for cx in range(COLS):
                if g.n[cx][cy] < 20:
                    near = [v for v, _ in vals]
                    g.p_goal[cx][cy] = round(sum(near) / len(near), 4)
        return g

    def value(self, x: Optional[float], y: Optional[float]) -> float:
        if x is None or y is None:
            return 0.0
        cx, cy = cell(x, y)
        return self.p_goal[cx][cy]

    def move_value(self, e: Event) -> float:
        """Threat gained or lost by one pass or carry, as the KQL MoveValue function computes it."""
        if e.type not in ("pass", "carry") or e.end_x is None:
            return 0.0
        start = self.value(e.x, e.y)
        if e.outcome == "complete":
            return self.value(e.end_x, e.end_y) - start
        return -start

    def rollout_value(self, events: list[Event], team_id: str, base_for: int, base_against: int, side: str) -> dict[str, float]:
        """Value of a branch for `team_id` over one rollout: the goal difference it produced, plus the
        shot xG and the threat created, minus the same for the opponent. Goals dominate; xG and threat
        separate branches that do not score within the horizon. Returns value and the threat part."""
        if not events:
            return {"value": 0.0, "threat": 0.0}
        last = events[-1]
        hf, ha = last.score_home, last.score_away
        gf = (hf if side == "home" else ha) - base_for
        ga = (ha if side == "home" else hf) - base_against
        xg = 0.0
        threat = 0.0
        for e in events:
            sign = 1.0 if e.team_id == team_id else -1.0
            if e.type == "shot":
                xg += sign * (e.xg or 0.0)
            elif e.type in ("pass", "carry"):
                threat += sign * self.move_value(e)
        return {"value": (gf - ga) + 0.5 * xg + 0.6 * threat, "threat": threat}

    def to_json(self) -> str:
        return json.dumps({"cols": COLS, "rows": ROWS, "p_goal": self.p_goal, "p_shot": self.p_shot, "n": self.n})

    @classmethod
    def from_json(cls, text: str) -> "ThreatGrid":
        d = json.loads(text)
        pg, ps, n = d["p_goal"], d["p_shot"], d["n"]
        if len(pg) == ROWS and len(pg[0]) == COLS:  # stored row-major: transpose to [cx][cy]
            t = lambda m: [[m[cy][cx] for cy in range(ROWS)] for cx in range(COLS)]
            pg, ps, n = t(pg), t(ps), t(n)
        if len(pg) != COLS or len(pg[0]) != ROWS:
            raise ValueError(f"threat grid must be {COLS}x{ROWS}, got {len(pg)}x{len(pg[0])}")
        return cls(pg, ps, n)

    def rows_for_kql(self) -> list[dict[str, Any]]:
        return [{"cx": cx, "cy": cy, "n": self.n[cx][cy], "p_shot": self.p_shot[cx][cy], "p_goal": self.p_goal[cx][cy]}
                for cy in range(ROWS) for cx in range(COLS) if self.n[cx][cy]]
