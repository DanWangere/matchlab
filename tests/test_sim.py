"""Properties the rest of the project depends on. Run with `python -m pytest`."""
import json
import random

import pytest

from matchlab.fork import action_events, fork, rebuild_to_event
from matchlab.league import League, make_league
from matchlab.schema import EVENT_TYPES, OUTCOMES, PHASES, Event
from matchlab.sim import Simulator
from matchlab.stats import summarize


@pytest.fixture(scope="module")
def league() -> League:
    return make_league(7)


@pytest.fixture(scope="module")
def match(league):
    sim = Simulator(league, "t03", "t09", 11)
    events = sim.run()
    return sim, events


def test_league_round_trips_through_json(league):
    again = League.from_json(league.to_json())
    assert [t.team_id for t in again.teams] == [t.team_id for t in league.teams]
    assert again.team("t05").player("t05p09").name == league.team("t05").player("t05p09").name
    for t in league.teams:
        assert len(t.starters()) == 11 and len(t.bench()) == 7
        assert t.starters()[0].role == "GK"


def test_determinism(league):
    a = Simulator(league, "t01", "t02", 3).run()
    b = Simulator(league, "t01", "t02", 3).run()
    assert [e.to_dict() for e in a] == [e.to_dict() for e in b]
    c = Simulator(league, "t01", "t02", 4).run()
    assert [e.to_dict() for e in a] != [e.to_dict() for e in c]


def test_event_contract(match):
    _, events = match
    ids = [e.event_id for e in events]
    assert ids == list(range(1, len(events) + 1)), "event ids are monotonic from 1"
    assert events[0].type == "kick_off"
    assert events[-1].type == "period_end" and events[-1].detail["period"] == 2
    assert sum(1 for e in events if e.type == "period_end") == 2
    for e in events:
        assert e.type in EVENT_TYPES
        assert e.outcome is None or e.outcome in OUTCOMES
        assert e.phase in PHASES
        assert e.period in (1, 2)
        if e.x is not None:
            assert 0 <= e.x <= 105 and 0 <= e.y <= 68
        if e.type == "pass":
            assert e.pass_length_m is not None and 0 <= e.pass_difficulty <= 1 and e.ball_speed_mps > 0
        if e.type == "shot":
            assert e.xg is not None and 0 < e.xg < 1 and e.ball_speed_mps > 0
    clocks = [e.clock_s for e in events if e.period == 1]
    assert clocks == sorted(clocks)
    # Period 2 restarts the match clock at 45:00.
    first_p2 = next(e for e in events if e.period == 2)
    assert first_p2.clock_s >= 2700
    json.loads(events[10].to_json())
    assert Event.from_dict(events[10].to_dict()) == Event(**events[10].to_dict())


def test_football_realism(match):
    sim, events = match
    s = summarize(events, sim.metadata())
    total_passes = sum(t["passes"] for t in s["teams"].values())
    total_shots = sum(t["shots"] for t in s["teams"].values())
    total_goals = sum(t["goals"] for t in s["teams"].values())
    assert 700 <= total_passes <= 1200
    assert 10 <= total_shots <= 45
    assert total_goals <= 9
    for t in s["teams"].values():
        assert 0.7 <= t["pass_completion"] <= 0.92
    assert 1200 <= s["events"] <= 2600


def test_score_is_consistent(match):
    _, events = match
    goals = [e for e in events if e.type == "shot" and e.outcome == "goal"]
    assert events[-1].score_home + events[-1].score_away == len(goals)
    for e in events:
        assert e.score_home >= 0 and e.score_away >= 0


def test_hidden_truth_logged(match):
    sim, _ = match
    minutes = sim.truth.minutes
    assert len(minutes) >= 2 * 90
    teams = {m["team_id"] for m in minutes}
    assert teams == {"t03", "t09"}
    for m in minutes:
        assert -1 <= m["momentum"] <= 1 and 0 <= m["fatigue"] <= 1
        assert 0 <= m["press_intent"] <= 1 and 0 <= m["risk_appetite"] <= 1
    subs = [e for e in sim.truth.minutes]  # noqa: F841 (shape check above)
    assert sum(1 for e in sim.truth.instruction_changes if e["cause"] == "substitution") <= 6
    doc = json.loads(sim.truth.to_json())
    assert set(doc) >= {"match_id", "schedules", "minutes", "instruction_changes", "policies"}


def test_snapshot_restore_is_exact(league):
    sim = Simulator(league, "t04", "t07", 21)
    for _ in range(300):
        sim.step()
    snap = sim.snapshot()
    branch_a = []
    for _ in range(50):
        branch_a += sim.step()
    sim.restore(snap)
    branch_b = []
    for _ in range(50):
        branch_b += sim.step()
    assert [e.to_dict() for e in branch_a] == [e.to_dict() for e in branch_b]


def test_rollout_leaves_state_untouched_and_forces_the_action(league):
    sim = Simulator(league, "t04", "t07", 21)
    while True:
        pre = sim.prepare()
        if sim.s.ready and sim.s.x > 70:
            break
        if sim.s.ready:
            sim.act()
    before = sim.snapshot()
    cands = sim.candidate_actions()
    shot = next(c for c in cands if c["type"] == "shot")
    sim.restore(before)
    out = sim.rollout(15, first_action=shot, salt=1)
    assert out and out[0].type == "shot"
    after = sim.snapshot()
    assert after["state"] == before["state"] and after["rng"] == before["rng"]
    # Different salts give different futures; the same salt gives the same future.
    again = sim.rollout(15, first_action=shot, salt=1)
    other = sim.rollout(15, first_action=shot, salt=2)
    assert [e.to_dict() for e in again] == [e.to_dict() for e in out]
    assert [e.to_dict() for e in other] != [e.to_dict() for e in out]


def test_fork_rebuilds_the_same_moment(league):
    sim = Simulator(league, "t02", "t06", 8)
    events = sim.run()
    meta = sim.metadata()
    target = max((e for e in action_events(events) if e.type == "shot"), key=lambda e: e.xg)
    rebuilt, ok = rebuild_to_event(league, meta, target.event_id)
    assert ok
    assert rebuilt.s.event_id + 1 == target.event_id
    assert rebuilt.s.carrier == target.player_id
    assert abs(rebuilt.s.x - target.x) < 1e-6 and abs(rebuilt.s.y - target.y) < 1e-6
    record = fork(rebuilt, n_rollouts=20, horizon=12)
    assert record["taken"]["type"] == "shot"
    assert abs(sum(c["p_policy"] for c in record["candidates"]) - 1) < 1e-6
    assert all(0 <= c["p_goal_for"] <= 1 for c in record["candidates"])
    # The fork leaves the simulator where it was: continuing reproduces the real match.
    cont = rebuilt.act()
    assert cont[0].to_dict() == target.to_dict()


def test_substitutions_keep_eleven_on_the_pitch(match):
    sim, events = match
    subs = [e for e in events if e.type == "substitution"]
    assert 1 <= len(subs) <= 6
    for side in ("home", "away"):
        ts = sim.ts(side)
        assert len(ts.on_pitch) == 11 and len(ts.slots) == 11
        assert set(ts.on_pitch) == set(ts.slots)
