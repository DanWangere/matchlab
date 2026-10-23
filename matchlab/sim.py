"""A deterministic, event-level football simulator with hidden state.

Design points that the rest of the project depends on:

* Deterministic: the same league, fixture and seed produce the same events.
* Snapshot/restore: the full match state (including the random generator) can be
  captured and restored, so any moment can be replayed with a different action.
  That is the simulator Otherwise borrows: rewind, force an alternative, roll out.
* Hidden variables: momentum, fatigue, press intent and risk appetite change the
  play minute by minute and are logged to a truth file that is never streamed.
  That is what Undertow is trained and scored against.
* Coordinates are always in the acting team's attack frame (towards x = 105).
"""
from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .league import League, Player, Team, role_group
from .schema import GOAL_Y, PITCH_LENGTH as L, PITCH_WIDTH as W, POST_HALF_WIDTH, Event
from .truth import MINUTES, TeamSchedule, TruthLog, make_schedule

# ----------------------------------------------------------------------------- helpers


def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def flip(x: float, y: float) -> tuple[float, float]:
    """Convert a point from one team's attack frame to the other's."""
    return L - x, W - y


def dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def zone(x: float) -> str:
    return "def" if x < 35 else ("mid" if x < 70 else "fin")


def goal_distance(x: float, y: float) -> float:
    return dist(x, y, L, GOAL_Y)


def goal_angle(x: float, y: float) -> float:
    """Angle subtended by the goal mouth, in radians."""
    dx = L - x
    if dx <= 0.1:
        return 0.0
    dy = y - GOAL_Y
    return math.atan2(2 * POST_HALF_WIDTH * dx, dx * dx + dy * dy - POST_HALF_WIDTH ** 2)


BASE_WEIGHTS: dict[str, dict[str, float]] = {
    "def": {"short": 0.62, "long": 0.11, "switch": 0.04, "carry": 0.18, "clearance": 0.05},
    "mid": {"short": 0.58, "long": 0.05, "switch": 0.05, "through": 0.06, "carry": 0.23, "shot": 0.004, "cross": 0.03},
    "fin": {"short": 0.43, "through": 0.11, "cross": 0.13, "carry": 0.23, "shot": 0.10},
}
TYPE_ADJ: dict[str, float] = {
    "short": 1.25, "long": 0.1, "through": -1.0, "cross": -1.3, "switch": 0.5,
    "corner": -1.3, "free_kick": 0.4, "goal_kick": 0.9, "throw_in": 0.9,
}
HOME_EDGE = 0.1  # added to the pass-completion logit and, scaled, to goal probability for the home side
RESTART_TIME: dict[str, float] = {"goal_kick": 18, "corner": 25, "free_kick": 20, "throw_in": 10, "kick_off": 8, "penalty": 40}
ZONE_PRESS: dict[str, float] = {"def": 1.0, "mid": 0.75, "fin": 0.6}


@dataclass
class Spot:
    """A player on the pitch: identity, current slot and the attributes the policy reads."""
    player_id: str
    role: str
    x: float
    y: float
    passing: float
    shooting: float
    pressing: float
    pace: float


@dataclass
class TeamState:
    team_id: str
    side: str
    on_pitch: list[str]
    bench: list[str]
    slots: dict[str, tuple[str, float, float]] = field(default_factory=dict)
    subs_used: int = 0
    goals: int = 0
    momentum: float = 0.0
    fatigue: float = 0.0
    fatigue_relief: float = 0.0
    press_intent: float = 0.5
    risk_appetite: float = 0.4
    adj_press: float = 0.0
    adj_risk: float = 0.0
    flags: dict[str, bool] = field(default_factory=dict)
    possession_s: float = 0.0


@dataclass
class MatchState:
    match_id: str
    teams: dict[str, TeamState]
    stoppage1: float
    stoppage2: float
    period: int = 1
    clock_s: float = 0.0
    possession: str = "home"
    carrier: Optional[str] = None
    x: float = 52.5
    y: float = GOAL_Y
    possession_id: int = 0
    possession_events: int = 0
    event_id: int = 0
    under_pressure: bool = False
    restart: Optional[str] = None
    restart_side: Optional[str] = None
    restart_xy: tuple[float, float] = (52.5, GOAL_Y)
    last_set_piece_event: Optional[int] = None
    last_cross_event: Optional[int] = None
    last_minute: int = -1
    kicked_off_first: str = "home"
    ended: bool = False
    ready: bool = False


class Simulator:
    """One match. Call `run()` for the whole thing, or `step()` event by event."""

    def __init__(self, league: League, home_id: str, away_id: str, seed: int,
                 match_id: Optional[str] = None, kickoff: Optional[datetime] = None):
        self.league = league
        self.home: Team = league.team(home_id)
        self.away: Team = league.team(away_id)
        self.seed = seed
        self.match_id = match_id or f"{self.home.short_name}-{self.away.short_name}-{seed}"
        self.kickoff = kickoff or datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
        self.rng = random.Random(f"{league.seed}:{home_id}:{away_id}:{seed}")
        schedules = {
            t.team_id: make_schedule(t.team_id, t.press_height, t.direct, self.rng) for t in (self.home, self.away)
        }
        self.truth = TruthLog(match_id=self.match_id, seed=seed, schedules=schedules)
        teams = {}
        for side, team in (("home", self.home), ("away", self.away)):
            teams[side] = TeamState(
                team.team_id, side, [p.player_id for p in team.starters()], [p.player_id for p in team.bench()],
                slots={p.player_id: (p.role, p.x, p.y) for p in team.starters()},
            )
        self.s = MatchState(
            match_id=self.match_id, teams=teams,
            stoppage1=self.rng.uniform(60, 180), stoppage2=self.rng.uniform(150, 360),
        )
        self.s.kicked_off_first = self.rng.choice(["home", "away"])
        self.s.restart, self.s.restart_side = "kick_off", self.s.kicked_off_first
        for side in ("home", "away"):
            self.truth.policies[self.s.teams[side].team_id] = self.policy_summary(side)

    # ------------------------------------------------------------------ accessors

    def team(self, side: str) -> Team:
        return self.home if side == "home" else self.away

    def ts(self, side: str) -> TeamState:
        return self.s.teams[side]

    @staticmethod
    def opp(side: str) -> str:
        return "away" if side == "home" else "home"

    def player(self, side: str, pid: str) -> Player:
        return self.team(side).player(pid)

    def policy_summary(self, side: str) -> dict[str, Any]:
        t = self.team(side)
        return {"possession": t.possession, "direct": t.direct, "press_height": t.press_height,
                "base_weights": BASE_WEIGHTS, "formation": t.formation}

    # ------------------------------------------------------------------ state capture

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": copy.deepcopy(self.s),
            "rng": self.rng.getstate(),
            "truth_minutes": len(self.truth.minutes),
            "truth_changes": len(self.truth.instruction_changes),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.s = copy.deepcopy(snap["state"])
        self.rng = random.Random()
        self.rng.setstate(snap["rng"])
        del self.truth.minutes[snap["truth_minutes"]:]
        del self.truth.instruction_changes[snap["truth_changes"]:]

    # ------------------------------------------------------------------ emission

    def _ts_iso(self) -> str:
        extra = 15 * 60 if self.s.period == 2 else 0
        return (self.kickoff + timedelta(seconds=self.s.clock_s + extra)).isoformat()

    def _phase(self) -> str:
        if self.s.last_set_piece_event is not None and self.s.event_id - self.s.last_set_piece_event <= 2:
            return "set_piece"
        if self.s.possession_events < 3:
            return "transition"
        z = zone(self.s.x)
        return {"def": "build_up", "mid": "progression", "fin": "final_third"}[z]

    def _emit(self, events: list[Event], etype: str, side: str, player_id: Optional[str], **kw: Any) -> Event:
        self.s.event_id += 1
        ev = Event(
            match_id=self.match_id, event_id=self.s.event_id, period=self.s.period, clock_s=self.s.clock_s,
            ts=self._ts_iso(), team_id=self.ts(side).team_id, player_id=player_id, type=etype,
            possession_id=f"{self.match_id}-{self.s.possession_id}", phase=self._phase(),
            score_home=self.ts("home").goals, score_away=self.ts("away").goals, **kw,
        )
        events.append(ev)
        return ev

    def _add_time(self, seconds: float) -> None:
        self.s.clock_s += seconds
        self.ts(self.s.possession).possession_s += seconds

    def _new_possession(self, side: str, carrier: Optional[str], x: float, y: float) -> None:
        self.s.possession = side
        self.s.carrier = carrier
        self.s.x, self.s.y = clamp(x, 0.5, L - 0.5), clamp(y, 0.5, W - 0.5)
        self.s.possession_id += 1
        self.s.possession_events = 0
        self.s.under_pressure = False

    def _change_possession(self, events: list[Event], new_side: str, carrier: Optional[str], x: float, y: float, cause: str) -> None:
        """x, y are already in the new side's frame."""
        self._new_possession(new_side, carrier, x, y)
        self._emit(events, "possession_change", new_side, carrier, x=self.s.x, y=self.s.y, detail={"cause": cause})

    # ------------------------------------------------------------------ players around the ball

    def _on_pitch(self, side: str) -> list[Spot]:
        team = self.team(side)
        out = []
        for pid, (role, x, y) in self.ts(side).slots.items():
            pl = team.player(pid)
            out.append(Spot(pid, role, x, y, pl.passing, pl.shooting, pl.pressing, pl.pace))
        return out

    def _nearest_opponent(self, side: str, x: float, y: float, exclude_gk: bool = True) -> Spot:
        fx, fy = flip(x, y)
        best, best_d = None, 1e9
        for p in self._on_pitch(self.opp(side)):
            if exclude_gk and p.role == "GK":
                continue
            d = dist(fx, fy, p.x, p.y) + self.rng.uniform(0, 6)
            if d < best_d:
                best, best_d = p, d
        assert best is not None
        return best

    def _pick(self, side: str, groups: tuple[str, ...], exclude: Optional[str] = None) -> Spot:
        pool = [p for p in self._on_pitch(side) if role_group(p.role) in groups and p.player_id != exclude]
        if not pool:
            pool = [p for p in self._on_pitch(side) if p.role != "GK"]
        return self.rng.choice(pool)

    def _any_pass(self, side: str, subtypes: tuple[str, ...]) -> tuple[str, float, float, float]:
        """A pass option for a restart: the first subtype with candidates, else a safe ball to a defender."""
        for st in subtypes:
            cands = self._pass_candidates(side, st)
            if cands:
                return self.rng.choice(cands)
        p = self._pick(side, ("DEF", "MID"), exclude=self.s.carrier)
        return p.player_id, 1.0, p.x + self.rng.gauss(0, 3), p.y + self.rng.gauss(0, 3)

    # ------------------------------------------------------------------ the minute tick

    def _tick(self, events: list[Event]) -> None:
        s = self.s
        minute = int(s.clock_s // 60)
        if minute != s.last_minute:
            for side in ("home", "away"):
                self._update_hidden(side, minute, events)
            s.last_minute = minute
        if s.period == 1 and s.clock_s >= 2700 + s.stoppage1:
            self._emit(events, "period_end", "home", None, detail={"period": 1})
            s.period = 2
            s.clock_s = 2700.0
            s.last_minute = 44
            s.restart, s.restart_side = "kick_off", self.opp(s.kicked_off_first)
        elif s.period == 2 and s.clock_s >= 5400 + s.stoppage2:
            self._emit(events, "period_end", "home", None, detail={"period": 2})
            s.ended = True

    def _update_hidden(self, side: str, minute: int, events: list[Event]) -> None:
        ts, other = self.ts(side), self.ts(self.opp(side))
        sch: TeamSchedule = self.truth.schedules[ts.team_id]
        m = min(minute, MINUTES - 1)
        for ch in sch.planned_changes:
            if ch["minute"] == minute:
                self.truth.log_change(minute, ts.team_id, "plan", ch["variable"], ch["delta"], "pre-planned tactical shift")
        diff = ts.goals - other.goals
        if minute >= 65 and diff < 0 and not ts.flags.get("trail_risk"):
            ts.flags["trail_risk"] = True
            ts.adj_risk += 0.2
            self.truth.log_change(minute, ts.team_id, "score_state", "risk_appetite", 0.2, "trailing after the hour")
        if minute >= 75 and diff > 0 and not ts.flags.get("lead_press"):
            ts.flags["lead_press"] = True
            ts.adj_press -= 0.15
            self.truth.log_change(minute, ts.team_id, "score_state", "press_intent", -0.15, "protecting a lead")
        if ts.subs_used < 3 and minute >= sch.sub_minutes[ts.subs_used]:
            self._substitute(side, minute, events)
        ts.press_intent = clamp(sch.press_intent[m] + ts.adj_press, 0.05, 0.95)
        ts.risk_appetite = clamp(sch.risk_appetite[m] + ts.adj_risk, 0.05, 0.95)
        ts.fatigue = clamp((minute / 95.0) ** 1.3 * (0.55 + 0.5 * ts.press_intent) - ts.fatigue_relief, 0.0, 1.0)
        ts.momentum = clamp(ts.momentum * 0.9 + self.rng.gauss(0, 0.07), -1.0, 1.0)
        self.truth.log_minute(minute, ts.team_id, ts.momentum, ts.fatigue, ts.press_intent, ts.risk_appetite)

    def _substitute(self, side: str, minute: int, events: list[Event]) -> None:
        ts = self.ts(side)
        team = self.team(side)
        outfield = [pid for pid in ts.on_pitch if team.player(pid).role != "GK"]
        off = self.rng.choice(outfield)
        off_group = role_group(team.player(off).role)
        same = [pid for pid in ts.bench if role_group(team.player(pid).role) == off_group]
        pool = same or [pid for pid in ts.bench if team.player(pid).role != "GK"]
        if not pool:
            return
        on = self.rng.choice(pool)
        ts.on_pitch[ts.on_pitch.index(off)] = on
        ts.bench.remove(on)
        ts.subs_used += 1
        ts.fatigue_relief += 0.06
        if self.s.carrier == off:
            self.s.carrier = on
        # The player coming on inherits the slot: same role and nominal position.
        slot = ts.slots.pop(off)
        ts.slots[on] = slot
        self._emit(events, "substitution", side, on, detail={"on": on, "off": off, "role": slot[0], "minute": minute})
        if self.rng.random() < 0.55:
            variable = self.rng.choice(["press_intent", "risk_appetite"])
            delta = self.rng.choice([-0.15, 0.15])
            if variable == "press_intent":
                ts.adj_press += delta
            else:
                ts.adj_risk += delta
            self.truth.log_change(minute, ts.team_id, "substitution", variable, delta, f"{off} off, {on} on")

    # ------------------------------------------------------------------ restarts

    def _do_restart(self, events: list[Event]) -> None:
        s = self.s
        rt, side = s.restart, s.restart_side
        assert rt and side
        s.restart = None
        self._add_time(RESTART_TIME[rt])
        if rt == "kick_off":
            taker = self._pick(side, ("MID",))
            self._new_possession(side, taker.player_id, 52.5, GOAL_Y)
            self._emit(events, "kick_off", side, taker.player_id, x=52.5, y=GOAL_Y)
            s.x, s.y = 50.0, GOAL_Y + self.rng.gauss(0, 3)
            return
        if rt == "penalty":
            taker = self._pick(side, ("FWD", "MID"))
            self._new_possession(side, taker.player_id, 94.0, GOAL_Y)
            s.last_set_piece_event = s.event_id
            self._shot({"type": "shot", "subtype": "penalty"}, events)
            return
        if rt == "goal_kick":
            gk = next(p for p in self._on_pitch(side) if p.role == "GK")
            self._new_possession(side, gk.player_id, 5.0, GOAL_Y)
            long = self.rng.random() < 0.35 + 0.3 * self.team(side).direct
            receiver = self._pick(side, ("MID", "FWD") if long else ("DEF",))
            action = {"type": "pass", "subtype": "goal_kick", "receiver": receiver.player_id,
                      "end_x": receiver.x + self.rng.gauss(0, 3), "end_y": receiver.y + self.rng.gauss(0, 3)}
        elif rt == "corner":
            taker = self._pick(side, ("MID", "FWD"))
            cy = 0.5 if s.restart_xy[1] < GOAL_Y else W - 0.5
            self._new_possession(side, taker.player_id, L - 0.5, cy)
            receiver = self._pick(side, ("FWD", "DEF"), exclude=taker.player_id)
            action = {"type": "pass", "subtype": "corner", "receiver": receiver.player_id,
                      "end_x": self.rng.gauss(97, 3), "end_y": self.rng.gauss(GOAL_Y, 6)}
        elif rt == "free_kick":
            taker = self._pick(side, ("MID", "DEF"))
            x, y = s.restart_xy
            self._new_possession(side, taker.player_id, x, y)
            s.last_set_piece_event = s.event_id
            if x > 72 and self.rng.random() < 0.3:
                self._shot({"type": "shot", "subtype": "set_piece"}, events)
                return
            order = ("short", "long") if self.rng.random() < 0.6 else ("long", "short")
            pid, _, ex, ey = self._any_pass(side, order)
            action = {"type": "pass", "subtype": "free_kick", "receiver": pid, "end_x": ex, "end_y": ey}
        else:  # throw_in
            taker = self._pick(side, ("DEF", "MID"))
            x, y = s.restart_xy
            self._new_possession(side, taker.player_id, x, 0.5 if y < GOAL_Y else W - 0.5)
            pid, _, ex, ey = self._any_pass(side, ("short", "long"))
            action = {"type": "pass", "subtype": "throw_in", "receiver": pid, "end_x": ex, "end_y": ey}
        s.last_set_piece_event = s.event_id
        self._resolve_pass(action, events)

    # ------------------------------------------------------------------ pressure

    def _maybe_pressure(self, events: list[Event]) -> None:
        s = self.s
        side = s.possession
        other = self.ts(self.opp(side))
        z = zone(s.x)
        p = (0.13 + 0.55 * other.press_intent * ZONE_PRESS[z]) * (1 - 0.45 * other.fatigue)
        s.under_pressure = self.rng.random() < p
        if not s.under_pressure:
            return
        presser = self._nearest_opponent(side, s.x, s.y)
        fx, fy = flip(s.x, s.y)
        subtype = "high_press" if z == "def" else ("counter_press" if s.possession_events < 3 else "mid_block")
        self._emit(events, "pressure", self.opp(side), presser.player_id, subtype=subtype, x=fx, y=fy)

    # ------------------------------------------------------------------ the policy

    def _pass_candidates(self, side: str, subtype: str) -> list[tuple[str, float, float, float]]:
        s = self.s
        out: list[tuple[str, float, float, float]] = []
        x, y = s.x, s.y
        for p in self._on_pitch(side):
            if p.player_id == s.carrier:
                continue
            d = dist(x, y, p.x, p.y)
            fwd = clamp((p.x - x) / 25.0, -1.0, 1.0)
            if subtype == "short":
                if p.role == "GK" and not (zone(x) == "def" and s.under_pressure):
                    continue
                # Team-mates come towards the ball to offer an option, so the short-pass target
                # sits between the nominal slot and the carrier.
                tx, ty = p.x + 0.35 * (x - p.x), p.y + 0.35 * (y - p.y)
                d = dist(x, y, tx, ty)
                if 3 <= d <= 28:
                    w = math.exp(-((d - 11) / 8) ** 2) * (1 + 0.5 * fwd)
                    out.append((p.player_id, w, tx + self.rng.gauss(0, 2.0), ty + self.rng.gauss(0, 2.0)))
            elif subtype == "long":
                if p.role != "GK" and 25 <= d <= 62:
                    w = math.exp(-((d - 38) / 12) ** 2) * (1 + 0.8 * max(0.0, fwd))
                    out.append((p.player_id, w, p.x + self.rng.gauss(0, 3), p.y + self.rng.gauss(0, 3)))
            elif subtype == "through":
                if role_group(p.role) == "FWD" and p.x > x + 5 and p.x > 55 and x > 40:
                    w = 1 + 0.5 * (p.x - x) / 30
                    out.append((p.player_id, w, min(100.0, p.x + 12 + self.rng.gauss(0, 3)), p.y + self.rng.gauss(0, 4)))
            elif subtype == "switch":
                if p.role != "GK" and abs(p.y - y) > 28 and 25 <= d <= 60:
                    out.append((p.player_id, 1.0, p.x + self.rng.gauss(0, 3), p.y + self.rng.gauss(0, 3)))
            elif subtype == "cross":
                if x > 68 and (y < 20 or y > 48) and (role_group(p.role) == "FWD" or (role_group(p.role) == "MID" and p.x > 55)):
                    out.append((p.player_id, 1 + p.shooting, self.rng.gauss(96, 3), self.rng.gauss(GOAL_Y, 6)))
        return out

    def candidate_actions(self) -> list[dict[str, Any]]:
        """Concrete actions available to the carrier now, each with its policy probability."""
        s = self.s
        side = s.possession
        team, ts = self.team(side), self.ts(side)
        z = zone(s.x)
        w = dict(BASE_WEIGHTS[z])
        r = ts.risk_appetite
        w["short"] *= 0.75 + 0.5 * team.possession
        if "long" in w:
            w["long"] *= (0.6 + 0.8 * r) * (0.7 + 0.6 * team.direct)
        if "through" in w:
            w["through"] *= 0.6 + 0.8 * r
        if "shot" in w:
            d = goal_distance(s.x, s.y)
            w["shot"] *= clamp(1.5 - d / 22.0, 0.08, 1.5) * ((0.5 + 0.9 * r) if d > 22 else 1.0)
        if "cross" in w and not (s.y < 20 or s.y > 48):
            w["cross"] *= 0.15
        if s.under_pressure:
            w["short"] *= 0.9
            w["carry"] *= 0.6
            if "long" in w:
                w["long"] *= 1.3
            if "clearance" in w:
                w["clearance"] *= 3.0
        cands: list[dict[str, Any]] = []
        for kind, weight in w.items():
            if weight <= 0:
                continue
            if kind in ("short", "long", "through", "switch", "cross"):
                opts = self._pass_candidates(side, kind)
                if not opts:
                    continue
                opts.sort(key=lambda o: -o[1])
                opts = opts[:3] if kind == "short" else opts[:1]
                total = sum(o[1] for o in opts)
                for pid, ow, ex, ey in opts:
                    cands.append({"type": "pass", "subtype": kind, "receiver": pid, "end_x": ex, "end_y": ey, "p": weight * ow / total})
            elif kind == "carry":
                dx = self.rng.uniform(4, 12) * (1.0 if s.x < 95 else 0.3)
                cands.append({"type": "carry", "end_x": s.x + dx, "end_y": s.y + self.rng.gauss(0, 3), "p": weight})
            elif kind == "shot":
                cands.append({"type": "shot", "subtype": "open_play", "p": weight})
            elif kind == "clearance":
                cands.append({"type": "clearance", "p": weight})
        total = sum(c["p"] for c in cands)
        for c in cands:
            c["p"] = c["p"] / total
        return cands

    def _sample_action(self) -> dict[str, Any]:
        cands = self.candidate_actions()
        u = self.rng.random()
        acc = 0.0
        for c in cands:
            acc += c["p"]
            if u <= acc:
                return c
        return cands[-1]

    # ------------------------------------------------------------------ resolution

    def _resolve(self, action: dict[str, Any], events: list[Event]) -> None:
        kind = action["type"]
        if kind == "pass":
            self._resolve_pass(action, events)
        elif kind == "carry":
            self._carry(action, events)
        elif kind == "shot":
            self._shot(action, events)
        elif kind == "clearance":
            self._clearance(events)
        else:
            raise ValueError(kind)

    def _completion(self, subtype: str, length: float, passing: float, under_pressure: bool) -> float:
        s = self.s
        side = s.possession
        team, ts = self.team(side), self.ts(side)
        z = 1.2 + TYPE_ADJ[subtype] + 3.5 * (passing - 0.65) - 0.03 * max(0.0, length - 12) \
            - (0.9 if under_pressure else 0.0) + 0.6 * (team.possession - 0.5) + 0.3 * ts.momentum \
            - 0.8 * ts.fatigue + (0.3 if zone(s.x) == "def" else 0.0) + (HOME_EDGE if side == "home" else 0.0)
        return sigmoid(z)

    def _resolve_pass(self, action: dict[str, Any], events: list[Event]) -> None:
        s = self.s
        side = s.possession
        passer = self.player(side, s.carrier) if s.carrier else self._pick(side, ("MID",))
        subtype = action["subtype"]
        ex, ey = clamp(action["end_x"], 0.5, L - 0.5), clamp(action["end_y"], 0.5, W - 0.5)
        length = dist(s.x, s.y, ex, ey)
        p_ref = self._completion(subtype, length, 0.65, s.under_pressure)
        p = self._completion(subtype, length, passer.passing, s.under_pressure)
        complete = self.rng.random() < p
        duration = 0.6 + length / 16.0
        ev = self._emit(events, "pass", side, passer.player_id, subtype=subtype, x=s.x, y=s.y, end_x=ex, end_y=ey,
                        outcome="complete" if complete else "incomplete", under_pressure=s.under_pressure,
                        pass_length_m=length, pass_difficulty=1 - p_ref, ball_speed_mps=length / duration)
        self._add_time(2.2 + length / 18.0)
        s.possession_events += 1
        s.under_pressure = False
        if subtype == "cross" or subtype == "corner":
            s.last_cross_event = ev.event_id
        if complete:
            s.carrier = action["receiver"]
            s.x, s.y = ex, ey
            return
        self._turnover(ex, ey, events)

    def _turnover(self, ex: float, ey: float, events: list[Event]) -> None:
        s = self.s
        side = s.possession
        other = self.opp(side)
        u = self.rng.random()
        fx, fy = flip(ex, ey)
        if u < 0.15:
            interceptor = self._nearest_opponent(side, ex, ey, exclude_gk=ex < 95)
            self._emit(events, "interception", other, interceptor.player_id, x=fx, y=fy, outcome="won")
            self._change_possession(events, other, interceptor.player_id, fx, fy, "interception")
            self._add_time(1.5)
            return
        if u < 0.75:
            recoverer = self._nearest_opponent(side, ex, ey, exclude_gk=ex < 95)
            self._change_possession(events, other, recoverer.player_id, fx, fy, "recovery")
            self._add_time(2.0)
            return
        if ex > 92 and self.rng.random() < 0.6:
            s.restart, s.restart_side, s.restart_xy = "goal_kick", other, (5.0, GOAL_Y)
        else:
            s.restart, s.restart_side, s.restart_xy = "throw_in", other, (clamp(fx, 5, 100), fy)
        self._change_possession(events, other, None, fx, fy, "out_of_play")

    def _carry(self, action: dict[str, Any], events: list[Event]) -> None:
        s = self.s
        side = s.possession
        carrier = self.player(side, s.carrier) if s.carrier else self._pick(side, ("MID",))
        ex, ey = clamp(action["end_x"], 0.5, L - 0.5), clamp(action["end_y"], 0.5, W - 0.5)
        d = dist(s.x, s.y, ex, ey)
        self._emit(events, "carry", side, carrier.player_id, x=s.x, y=s.y, end_x=ex, end_y=ey, outcome="complete",
                   under_pressure=s.under_pressure)
        self._add_time(2.0 + d / 4.0)
        s.possession_events += 1
        s.x, s.y = ex, ey
        in_box = ex > 88 and 14 < ey < 54
        attempt = (0.5 if s.under_pressure else 0.09) * (0.6 if in_box else 1.0)
        if self.rng.random() < attempt:
            tackler = self._nearest_opponent(side, ex, ey)
            other = self.opp(side)
            ots = self.ts(other)
            fx, fy = flip(ex, ey)
            # Defenders are far more careful inside their own box.
            if self.rng.random() < (0.08 if in_box else 0.4):
                foul = self._emit(events, "foul", other, tackler.player_id, x=fx, y=fy)
                if self.rng.random() < 0.12:
                    self._emit(events, "card", other, tackler.player_id, x=fx, y=fy, related_event_id=foul.event_id, detail={"card": "yellow"})
                if in_box:
                    s.restart, s.restart_side = "penalty", side
                else:
                    s.restart, s.restart_side, s.restart_xy = "free_kick", side, (ex, ey)
                s.under_pressure = False
                return
            p_win = clamp(0.4 + 0.5 * (tackler.pressing - carrier.pace) + 0.15 * ots.press_intent
                          + 0.1 * ots.momentum - 0.2 * ots.fatigue, 0.1, 0.8)
            if self.rng.random() < p_win:
                self._emit(events, "tackle", other, tackler.player_id, x=fx, y=fy, outcome="won")
                self._change_possession(events, other, tackler.player_id, fx, fy, "tackle")
            else:
                self._emit(events, "tackle", other, tackler.player_id, x=fx, y=fy, outcome="lost")
        s.under_pressure = False

    def _clearance(self, events: list[Event]) -> None:
        s = self.s
        side = s.possession
        other = self.opp(side)
        clearer = self.player(side, s.carrier) if s.carrier else self._pick(side, ("DEF",))
        ex, ey = clamp(s.x + self.rng.uniform(25, 45), 0.5, L - 0.5), self.rng.uniform(4, W - 4)
        self._emit(events, "clearance", side, clearer.player_id, x=s.x, y=s.y, end_x=ex, end_y=ey, outcome="complete",
                   under_pressure=s.under_pressure, ball_speed_mps=self.rng.uniform(18, 28))
        self._add_time(3.0)
        s.under_pressure = False
        fx, fy = flip(ex, ey)
        if self.rng.random() < 0.65:
            receiver = self._nearest_opponent(side, ex, ey)
            self._change_possession(events, other, receiver.player_id, fx, fy, "clearance")
        else:
            s.restart, s.restart_side, s.restart_xy = "throw_in", other, (clamp(fx, 5, 100), fy)
            self._change_possession(events, other, None, fx, fy, "out_of_play")

    def _shot(self, action: dict[str, Any], events: list[Event]) -> None:
        s = self.s
        side = s.possession
        other = self.opp(side)
        ts, ots = self.ts(side), self.ts(other)
        shooter = self.player(side, s.carrier) if s.carrier else self._pick(side, ("FWD",))
        subtype = action.get("subtype", "open_play")
        related = None
        if subtype == "open_play" and s.last_cross_event is not None and s.event_id - s.last_cross_event <= 1:
            subtype = "header" if self.rng.random() < 0.7 else "open_play"
            related = s.last_cross_event
        if subtype == "penalty":
            xg = 0.76
            x, y = 94.0, GOAL_Y
        else:
            x, y = s.x, s.y
            xg = sigmoid(-1.2 - 0.105 * goal_distance(x, y) + 1.5 * goal_angle(x, y) + 0.9 * (shooter.shooting - 0.6))
            if subtype == "header":
                xg *= 0.7
            if subtype == "set_piece":
                xg *= 0.9
            if s.under_pressure:
                xg *= 0.78
        xg = clamp(xg, 0.005, 0.95)
        p_goal = clamp(xg * (1 + 0.15 * ts.momentum - 0.1 * ts.fatigue + (HOME_EDGE if side == "home" else 0.0)), 0.005, 0.95)
        u = self.rng.random()
        if u < p_goal:
            outcome = "goal"
        else:
            v = self.rng.random()
            outcome = "saved" if v < 0.3 else ("blocked" if v < 0.6 else "off_target")
        speed = self.rng.uniform(9, 16) if subtype == "header" else self.rng.uniform(18, 31)
        ev = self._emit(events, "shot", side, shooter.player_id, subtype=subtype, x=x, y=y, end_x=L, end_y=GOAL_Y + self.rng.gauss(0, 2.5),
                        outcome=outcome, under_pressure=s.under_pressure, xg=xg, ball_speed_mps=speed, related_event_id=related)
        s.possession_events += 1
        s.under_pressure = False
        s.last_cross_event = None
        if outcome == "goal":
            ts.goals += 1
            ev.score_home, ev.score_away = self.ts("home").goals, self.ts("away").goals
            ts.momentum = clamp(ts.momentum + 0.35, -1, 1)
            ots.momentum = clamp(ots.momentum - 0.35, -1, 1)
            self._add_time(65.0)
            s.restart, s.restart_side = "kick_off", other
            self._change_possession(events, other, None, 52.5, GOAL_Y, "goal")
        elif outcome == "saved":
            self._add_time(5.0)
            gk = next(p for p in self._on_pitch(other) if p.role == "GK")
            self._change_possession(events, other, gk.player_id, 6.0, GOAL_Y, "save")
        elif outcome == "blocked":
            self._add_time(3.0)
            if self.rng.random() < 0.45:
                s.restart, s.restart_side, s.restart_xy = "corner", side, (L - 0.5, y)
                s.possession_events = 0
            else:
                blocker = self._nearest_opponent(side, x, y)
                fx, fy = flip(x - 4, y)
                self._change_possession(events, other, blocker.player_id, fx, fy, "block")
        else:
            self._add_time(4.0)
            s.restart, s.restart_side, s.restart_xy = "goal_kick", other, (5.0, GOAL_Y)
            self._change_possession(events, other, None, 5.0, GOAL_Y, "out_of_play")

    # ------------------------------------------------------------------ stepping

    def prepare(self) -> list[Event]:
        """Advance through the clock tick, any pending restart and the pressure check.
        Leaves the match either ready for an action (`self.s.ready`) or not (restart, period end)."""
        events: list[Event] = []
        s = self.s
        s.ready = False
        if s.ended:
            return events
        self._tick(events)
        if s.ended:
            return events
        if s.restart:
            self._do_restart(events)
            return events
        self._maybe_pressure(events)
        s.ready = True
        return events

    def act(self, action: Optional[dict[str, Any]] = None) -> list[Event]:
        """Resolve one action from the ready state. `action` overrides the policy (a counterfactual)."""
        events: list[Event] = []
        if not self.s.ready:
            return events
        self._resolve(action or self._sample_action(), events)
        self.s.ready = False
        return events

    def step(self, forced: Optional[dict[str, Any]] = None) -> list[Event]:
        events = self.prepare()
        if self.s.ready:
            events += self.act(forced)
        return events

    def run(self) -> list[Event]:
        events: list[Event] = []
        while not self.s.ended:
            events += self.step()
        return events

    def rollout(self, n_events: int, first_action: Optional[dict[str, Any]] = None, salt: int = 0) -> list[Event]:
        """From a ready state: take `first_action` (or the policy's choice), continue for about
        `n_events` events, return them, and put the match back exactly where it was."""
        snap = self.snapshot()
        self.rng = random.Random(f"{self.seed}:{self.s.event_id}:{salt}")
        out = self.act(first_action)
        while len(out) < n_events and not self.s.ended:
            out += self.step()
        self.restore(snap)
        return out

    # ------------------------------------------------------------------ metadata

    def metadata(self) -> dict[str, Any]:
        def lineup(team: Team) -> list[dict[str, Any]]:
            return [{"player_id": p.player_id, "name": p.name, "shirt": p.shirt, "role": p.role,
                     "x": p.x, "y": p.y, "starter": p.starter} for p in team.players]
        return {
            "match_id": self.match_id, "seed": self.seed, "league_seed": self.league.seed,
            "kickoff": self.kickoff.isoformat(), "venue": self.home.stadium,
            "home": {"team_id": self.home.team_id, "name": self.home.name, "short_name": self.home.short_name,
                     "formation": self.home.formation, "lineup": lineup(self.home)},
            "away": {"team_id": self.away.team_id, "name": self.away.name, "short_name": self.away.short_name,
                     "formation": self.away.formation, "lineup": lineup(self.away)},
            "pitch": {"length": L, "width": W, "frame": "each event is in the acting team's attack frame, towards x=105"},
        }
