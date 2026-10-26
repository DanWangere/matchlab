"""Keep a simulator in step with a live event stream, and fork at the moments that matter.

The generator is deterministic, so a Follower rebuilt from the match metadata reproduces the
stream exactly: every event the stream delivers is one the follower's own simulator is about to
produce. The follower advances its simulator one step for each incoming event, checks they agree,
and whenever the next event will be a fork-worthy decision it forks first (rollouts from the
ready state), then lets the real event through.

Latency per fork is the rollout cost: 200 rollouts x 5 candidates x 20 events is about 1.5 s on a
laptop. The fork record is ready before the next phase of play.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from .cues import Cue, cue_from_fork
from .fork import fork
from .league import League
from .schema import Event
from .sim import Simulator, goal_distance
from .threat import ThreatGrid

FORKABLE = ("pass", "carry", "shot", "clearance")
RESTART_SUBTYPES = ("corner", "free_kick", "goal_kick", "throw_in", "penalty", "set_piece")


@dataclass
class ForkPolicy:
    """What counts as fork-worthy. Every shot; a final-third on-ball action where the carrier had a
    through ball, cross or shot available; and nothing more often than `min_gap_s` apart."""
    every_shot: bool = True
    final_third_x: float = 70.0
    min_gap_s: float = 45.0
    max_per_match: int = 25
    rollouts: int = 200
    horizon: int = 20

    def wants(self, sim: Simulator, next_type: str, next_subtype: Optional[str], last_fork_clock: float, forks_so_far: int) -> bool:
        if forks_so_far >= self.max_per_match:
            return False
        if next_type not in FORKABLE or next_subtype in RESTART_SUBTYPES:
            return False
        s = sim.s
        if next_type == "shot" and self.every_shot:
            return True
        if s.clock_s - last_fork_clock < self.min_gap_s:
            return False
        if s.x >= self.final_third_x:
            kinds = {c["type"] + ":" + str(c.get("subtype")) for c in sim.candidate_actions()}
            return bool(kinds & {"shot:open_play", "pass:through", "pass:cross"})
        return False


@dataclass
class FollowStats:
    events_seen: int = 0
    mismatches: int = 0
    forks: int = 0
    fork_seconds: float = 0.0
    last_fork_clock: float = -1e9


class Follower:
    """One per match. Feed it events in stream order with `on_event`; it returns the fork records
    produced (zero or one per event)."""

    def __init__(self, league: League, meta: dict[str, Any], policy: Optional[ForkPolicy] = None,
                 grid: Optional[ThreatGrid] = None, on_fork: Optional[Callable[[dict[str, Any], Cue], None]] = None):
        self.meta = meta
        self.policy = policy or ForkPolicy()
        self.grid = grid
        self.on_fork = on_fork
        self.sim = Simulator(league, meta["home"]["team_id"], meta["away"]["team_id"], meta["seed"],
                             match_id=meta["match_id"], kickoff=datetime.fromisoformat(meta["kickoff"]))
        self.stats = FollowStats()
        self.pending: list[Event] = []  # events the sim produced that the stream has not yet confirmed
        self.records: list[dict[str, Any]] = []
        self.done = False

    # ------------------------------------------------------------------ stepping

    def _produce_until(self, event_id: int) -> None:
        """Advance the simulator until it has produced the event with this id, forking first when
        the policy wants the decision that produces it."""
        # Always read through self.sim.s: restore() swaps the state object, so a local alias goes stale.
        while self.sim.s.event_id < event_id and not self.sim.s.ended:
            pre = self.sim.prepare()
            self.pending += pre
            if not self.sim.s.ready:
                continue
            if self.sim.s.event_id + 1 == event_id:
                # The next act() produces the event we are waiting for. Fork now if it matters.
                self._maybe_fork(event_id)
            self.pending += self.sim.act()

    def _maybe_fork(self, event_id: int) -> None:
        # Everything that peeks at the policy draws random numbers (candidate end points, the action
        # draw), so the whole decision runs under one snapshot and is restored before the real act().
        snap = self.sim.snapshot()
        cands = self.sim.candidate_actions()
        u = self.sim.rng.random()
        acc, nxt = 0.0, cands[-1]
        for c in cands:
            acc += c["p"]
            if u <= acc:
                nxt = c
                break
        wanted = self.policy.wants(self.sim, nxt["type"], nxt.get("subtype"), self.stats.last_fork_clock, self.stats.forks)
        self.sim.restore(snap)
        if not wanted:
            return
        t0 = time.perf_counter()
        record = fork(self.sim, n_rollouts=self.policy.rollouts, horizon=self.policy.horizon, grid=self.grid)
        record["fork_ms"] = round((time.perf_counter() - t0) * 1000)
        self.stats.forks += 1
        self.stats.fork_seconds += time.perf_counter() - t0
        self.stats.last_fork_clock = self.sim.s.clock_s
        self.records.append(record)
        if self.on_fork:
            self.on_fork(record, cue_from_fork(record, self.meta))

    def on_event(self, ev: Event) -> Optional[dict[str, Any]]:
        """Consume one stream event. Returns the fork record if this event was forked."""
        if self.done:
            return None
        before = len(self.records)
        self.stats.events_seen += 1
        if not any(p.event_id == ev.event_id for p in self.pending):
            self._produce_until(ev.event_id)
        mine = next((p for p in self.pending if p.event_id == ev.event_id), None)
        if mine is None or mine.type != ev.type or mine.team_id != ev.team_id or abs((mine.x or 0) - (ev.x or 0)) > 0.05:
            self.stats.mismatches += 1
        self.pending = [p for p in self.pending if p.event_id > ev.event_id]
        if ev.type == "period_end" and ev.detail.get("period") == 2:
            self.done = True
        return self.records[-1] if len(self.records) > before else None

    def flush(self) -> None:
        """Run the simulator to the end (for scoring after a stream stops early)."""
        while not self.sim.s.ended:
            self.pending += self.sim.step()
