"""Otherwise, in miniature: rewind a match to the moment before a decision, replay every
plausible alternative many times, and price the gap. This is the generator-as-simulator
shortcut: no learned event model is needed to get a calibrated counterfactual under the
league's own policy.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .league import League
from .schema import Event
from .sim import Simulator


def rebuild_to_event(league: League, meta: dict[str, Any], event_id: int) -> tuple[Simulator, bool]:
    """Re-simulate deterministically to the ready state whose next action produced `event_id`.
    Returns (sim, forkable). Not forkable when the event came from a restart, a tick, or was
    not the first event of its action (an interception after a pass, say)."""
    sim = Simulator(league, meta["home"]["team_id"], meta["away"]["team_id"], meta["seed"],
                    match_id=meta["match_id"], kickoff=datetime.fromisoformat(meta["kickoff"]))
    while not sim.s.ended:
        snap = sim.snapshot()
        pre = sim.prepare()
        if any(e.event_id == event_id for e in pre):
            sim.restore(snap)
            return sim, False
        if sim.s.ready:
            if sim.s.event_id + 1 == event_id:
                return sim, True
            sim.act()
            if sim.s.event_id >= event_id:
                return sim, False
    return sim, False


def describe(sim: Simulator, action: dict[str, Any]) -> dict[str, Any]:
    side = sim.s.possession
    d: dict[str, Any] = {"type": action["type"], "subtype": action.get("subtype"), "p_policy": round(action.get("p", 0.0), 3)}
    if "receiver" in action:
        p = sim.player(side, action["receiver"])
        d["receiver"] = action["receiver"]
        d["receiver_name"] = p.name
    for k in ("end_x", "end_y"):
        if k in action:
            d[k] = round(action[k], 1)
    return d


def fork(sim: Simulator, n_rollouts: int = 200, horizon: int = 20) -> dict[str, Any]:
    """From a ready state, evaluate every candidate action by rollout. Leaves the match where it was."""
    assert sim.s.ready, "fork() needs a ready state: call prepare() first"
    s = sim.s
    side = s.possession
    ts = sim.ts(side)
    base_for, base_against = ts.goals, sim.ts(sim.opp(side)).goals
    snap = sim.snapshot()
    cands = sim.candidate_actions()
    u = sim.rng.random()
    acc, taken = 0.0, cands[-1]
    for c in cands:
        acc += c["p"]
        if u <= acc:
            taken = c
            break
    sim.restore(snap)

    results = []
    for ci, c in enumerate(cands):
        goals_for = goals_against = 0
        xg_for = 0.0
        for k in range(n_rollouts):
            evs = sim.rollout(horizon, first_action=c, salt=ci * 100000 + k)
            if not evs:
                continue
            last = evs[-1]
            hf, ha = last.score_home, last.score_away
            gf = (hf if side == "home" else ha) - base_for
            ga = (ha if side == "home" else hf) - base_against
            goals_for += 1 if gf > 0 else 0
            goals_against += 1 if ga > 0 else 0
            xg_for += sum((e.xg or 0.0) for e in evs if e.type == "shot" and e.team_id == ts.team_id)
        n = max(1, n_rollouts)
        r = describe(sim, c)
        r.update({
            "p_goal_for": round(goals_for / n, 3), "p_goal_against": round(goals_against / n, 3),
            "xg_for": round(xg_for / n, 3), "value": round((goals_for - goals_against) / n, 3),
            "taken": c is taken,
        })
        results.append(r)
    results.sort(key=lambda r: -r["value"])
    taken_r = next(r for r in results if r["taken"])
    best_untaken = next((r for r in results if not r["taken"]), None)
    carrier = sim.player(side, s.carrier) if s.carrier else None
    return {
        "match_id": sim.match_id, "event_id": s.event_id + 1, "period": s.period, "clock_s": round(s.clock_s, 1),
        "minute": int(s.clock_s // 60) + 1, "side": side, "team_id": ts.team_id,
        "player_id": s.carrier, "player_name": carrier.name if carrier else None,
        "x": round(s.x, 1), "y": round(s.y, 1), "under_pressure": s.under_pressure,
        "score": {"home": sim.ts("home").goals, "away": sim.ts("away").goals},
        "rollouts": n_rollouts, "horizon_events": horizon,
        "taken": taken_r, "best_untaken": best_untaken,
        "regret": round((best_untaken["value"] - taken_r["value"]) if best_untaken else 0.0, 3),
        "candidates": results,
    }


def action_events(events: list[Event]) -> list[Event]:
    """Events that were the first thing a carrier did with the ball: the forkable ones."""
    return [e for e in events if e.type in ("pass", "carry", "shot", "clearance")
            and e.subtype not in ("corner", "free_kick", "goal_kick", "throw_in", "penalty", "set_piece")]
