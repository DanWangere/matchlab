"""The cue contract and the hub."""
import json
import threading
import time
import urllib.request

import pytest

from matchlab.cues import LANGS, LAYERS, MODES, Cue, cue_from_fork, cues_from_events
from matchlab.fork import action_events, fork, rebuild_to_event
from matchlab.league import make_league
from matchlab.serve import HUB, Handler, build_cues
from matchlab.sim import Simulator


@pytest.fixture(scope="module")
def played():
    league = make_league(7)
    sim = Simulator(league, "t04", "t16", 1002)
    events = sim.run()
    return league, sim, events, sim.metadata()


def test_baseline_cues_cover_the_match(played):
    league, sim, events, meta = played
    cues = cues_from_events(events, meta)
    assert cues
    layers = {c.layer for c in cues}
    assert {"lower_third", "dial"} <= layers <= set(LAYERS)
    goals = [e for e in events if e.type == "shot" and e.outcome == "goal"]
    assert sum(1 for c in cues if c.layer == "lower_third" and c.text["en"].startswith("GOAL")) == len(goals)
    assert sum(1 for c in cues if c.text["en"].startswith("Full-time")) == 1
    for c in cues:
        assert set(c.text) == set(LANGS) and all(c.text.values())
        assert set(c.modes) <= set(MODES) and c.modes
        assert c.period in (1, 2) and c.duration_s > 0
        d = json.loads(c.to_json())
        assert d["cue_id"] == c.cue_id
    # Sorted on the match clock, period first.
    keys = [(c.period, c.t_start_s) for c in cues]
    assert keys == sorted(keys)


def test_dial_matches_the_scoreline_direction(played):
    _, sim, events, meta = played
    dials = [c for c in cues_from_events(events, meta) if c.layer == "dial"]
    assert 85 <= len(dials) <= 110
    for c in dials:
        v = c.geometry["value"]
        assert -1 <= v <= 1
        if abs(v) > 0.2:
            assert c.text["en"] != "Evenly matched"


def test_fork_cue_carries_the_ghost(played):
    league, sim, events, meta = played
    shot = max((e for e in action_events(events) if e.type == "shot"), key=lambda e: e.xg)
    rebuilt, ok = rebuild_to_event(league, meta, shot.event_id)
    assert ok
    record = fork(rebuilt, n_rollouts=20, horizon=12)
    c = cue_from_fork(record, meta)
    assert c.layer == "ghost" and c.source == "otherwise"
    assert c.geometry["side"] in ("home", "away") and "candidates" in c.geometry
    assert c.event_id == shot.event_id and c.t_start_s > shot.clock_s
    for lang in LANGS:
        assert c.text[lang] and c.text_analyst[lang]
        assert "%" in c.text_analyst[lang]


def test_build_cues_adds_forks(played):
    league, sim, events, meta = played
    cues = build_cues(events, meta, league, forks=2, rollouts=10, horizon=8)
    assert sum(1 for c in cues if c.layer == "ghost") == 2


def test_hub_round_trip(played):
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _, sim, events, meta = played
    try:
        def post(path, body):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        post("/meta", meta)
        assert post("/events", [e.to_dict() for e in events[:5]])["accepted"] == 5
        cue = cues_from_events(events, meta)[0]
        assert post("/cues", [cue.to_dict()])["accepted"] == 1
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=5) as r:
            state = json.loads(r.read())
        assert state["meta"]["match_id"] == meta["match_id"] and len(state["cues"]) == 1
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            assert b"<svg id=\"pitch\"" in r.read()
        # A late subscriber gets the buffered meta, events and cues.
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/stream", timeout=5)
        seen = b""
        t0 = time.time()
        while b"event: cue" not in seen and time.time() - t0 < 5:
            seen += resp.fp.read1(4096)
        assert b"event: meta" in seen and b"event: event" in seen and b"event: cue" in seen
    finally:
        server.shutdown()
