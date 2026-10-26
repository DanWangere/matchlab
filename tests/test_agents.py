"""The agent team, offline: the Verifier catches what it must, the pipeline swaps in model text
only when it verifies, and the fake model keeps the whole thing runnable without Foundry."""
import json

import pytest

from matchlab.agents import FakeModel, Pipeline, Verifier, _slim
from matchlab.cues import Names
from matchlab.fork import action_events, fork, rebuild_to_event
from matchlab.league import make_league
from matchlab.sim import Simulator
from matchlab.threat import ThreatGrid


@pytest.fixture(scope="module")
def record_and_meta():
    league = make_league(7)
    sim = Simulator(league, "t02", "t07", 55)
    events = sim.run()
    meta = sim.metadata()
    grid = ThreatGrid.learn([events])
    shot = max((e for e in action_events(events) if e.type == "shot"), key=lambda e: e.xg)
    rebuilt, ok = rebuild_to_event(league, meta, shot.event_id)
    assert ok
    return fork(rebuilt, n_rollouts=12, horizon=10, grid=grid), meta


def test_verifier_accepts_record_facts_and_rejects_others(record_and_meta):
    record, meta = record_and_meta
    v = Verifier(record, Names(meta))
    name = record["player_name"]
    p = round(record["taken"]["p_goal_for"] * 100)
    ok, problems = v.check(f"{name} shot: {p}% chance of a goal within {record['horizon_events']} events. Regret {record['regret']:+.2f}.")
    assert ok, problems
    ok, problems = v.check(f"{name} has scored 14 goals this season.")
    assert not ok and any("14" in x for x in problems)
    ok, problems = v.check(f"{name} found Marcus Rashford with a pass.")
    assert not ok and any("Rashford" in x for x in problems)
    # Sentence-initial capitalised words are not names.
    ok, _ = v.check("Carrying on was the better option.")
    assert ok


def test_pipeline_uses_model_text_when_it_verifies(record_and_meta):
    record, meta = record_and_meta
    pipe = Pipeline(FakeModel(), meta)
    cue, ex = pipe.cue(record)
    assert ex.model_calls == 2
    assert all(ex.verified.values())
    assert cue.geometry["text_source"] == "model"
    assert cue.text["en"] == ex.text["en"] and cue.text_analyst["fr"] == ex.text_analyst["fr"]
    assert record["player_name"] in cue.text["es"]


def test_pipeline_falls_back_on_hallucination(record_and_meta):
    record, meta = record_and_meta
    pipe = Pipeline(FakeModel(hallucinate=True), meta)
    cue, ex = pipe.cue(record)
    assert ex.fallback and not any(ex.verified.values())
    assert cue.geometry["text_source"] == "template"
    assert "14" not in cue.text_analyst["en"]
    assert any("14" in p for p in ex.problems["en"])


def test_pipeline_survives_a_model_outage(record_and_meta):
    record, meta = record_and_meta

    class Down:
        calls = 0
        def complete(self, *a, **k):
            raise ConnectionError("model unreachable")

    cue, ex = Pipeline(Down(), meta).cue(record)
    assert ex.fallback and cue.geometry["text_source"] == "template"
    assert cue.text["en"] and cue.text["fr"] and cue.text["es"]


def test_slim_record_keeps_names_drops_geometry(record_and_meta):
    record, meta = record_and_meta
    s = _slim(record)
    assert "player_name" in s and "candidates" in s
    assert "x" not in s and "actual_event" not in s
    json.dumps(s)


def test_recap_is_verified(record_and_meta):
    record, meta = record_and_meta
    pipe = Pipeline(FakeModel(), meta)
    out = pipe.recap([record], {"home": 1, "away": 0})
    assert all(out["verified"].values())
    assert str(record["minute"]) in out["text"]["en"]
    assert len(out["forks"]) == 1
