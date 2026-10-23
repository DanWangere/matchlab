"""Match summaries: the numbers to check realism against a real match-level dataset."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .schema import Event


def summarize(events: list[Event], meta: dict[str, Any]) -> dict[str, Any]:
    home, away = meta["home"]["team_id"], meta["away"]["team_id"]
    per: dict[str, dict[str, float]] = {home: defaultdict(float), away: defaultdict(float)}
    types = Counter(e.type for e in events)
    pass_by_sub: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in events:
        t = per[e.team_id]
        if e.type == "pass":
            t["passes"] += 1
            t["passes_complete"] += 1 if e.outcome == "complete" else 0
            t["pass_length_sum"] += e.pass_length_m or 0
            t["pass_difficulty_sum"] += e.pass_difficulty or 0
            pass_by_sub[e.subtype or "?"][0] += 1
            pass_by_sub[e.subtype or "?"][1] += 1 if e.outcome == "complete" else 0
        elif e.type == "shot":
            t["shots"] += 1
            t["xg"] += e.xg or 0
            t["on_target"] += 1 if e.outcome in ("goal", "saved") else 0
            t["goals"] += 1 if e.outcome == "goal" else 0
        elif e.type == "pressure":
            t["pressures"] += 1
        elif e.type == "tackle":
            t["tackles"] += 1
            t["tackles_won"] += 1 if e.outcome == "won" else 0
        elif e.type == "interception":
            t["interceptions"] += 1
        elif e.type == "carry":
            t["carries"] += 1
    last = events[-1]
    out: dict[str, Any] = {
        "match_id": meta["match_id"],
        "score": f"{last.score_home}-{last.score_away}",
        "events": len(events),
        "by_type": dict(types),
        "pass_completion_by_subtype": {k: round(v[1] / v[0], 3) for k, v in pass_by_sub.items() if v[0]},
        "teams": {},
    }
    for tid, t in per.items():
        out["teams"][tid] = {
            "passes": int(t["passes"]),
            "pass_completion": round(t["passes_complete"] / t["passes"], 3) if t["passes"] else None,
            "avg_pass_length_m": round(t["pass_length_sum"] / t["passes"], 1) if t["passes"] else None,
            "avg_pass_difficulty": round(t["pass_difficulty_sum"] / t["passes"], 3) if t["passes"] else None,
            "shots": int(t["shots"]), "on_target": int(t["on_target"]), "xg": round(t["xg"], 2), "goals": int(t["goals"]),
            "pressures": int(t["pressures"]), "tackles": int(t["tackles"]), "tackles_won": int(t["tackles_won"]),
            "interceptions": int(t["interceptions"]), "carries": int(t["carries"]),
        }
    return out
