"""The fork service: the follower tracks a stream exactly, forks where the policy says, and the
inputs decode what the stream carries."""
import json
import threading
from datetime import datetime, timezone

import pytest

from matchlab.follow import Follower, ForkPolicy
from matchlab.forkd import ForkService, MetaStore, Outputs, eventhouse_events, file_events
from matchlab.league import make_league
from matchlab.schema import Event
from matchlab.serve import HUB, Handler
from matchlab.sim import Simulator
from matchlab.threat import COLS, ROWS, ThreatGrid, cell


@pytest.fixture(scope="module")
def league():
    return make_league(7)


@pytest.fixture(scope="module")
def matches(league):
    out = []
    for i, (h, a) in enumerate([("t01", "t02"), ("t03", "t04"), ("t05", "t06")]):
        sim = Simulator(league, h, a, 300 + i)
        out.append((sim.run(), sim.metadata()))
    return out


@pytest.fixture(scope="module")
def grid(matches):
    return ThreatGrid.learn(evs for evs, _ in matches)


def test_threat_grid_shape_and_gradient(grid):
    assert len(grid.p_goal) == COLS and all(len(col) == ROWS for col in grid.p_goal)
    centre = [grid.p_goal[cx][ROWS // 2] for cx in range(COLS)]
    assert centre[-1] > centre[COLS // 2] > 0, "danger must rise towards the goal"
    assert cell(-3.0, 70.0) == (0, ROWS - 1) and cell(105.0, 34.0) == (COLS - 1, ROWS // 2)
    again = ThreatGrid.from_json(grid.to_json())
    assert again.p_goal == grid.p_goal
    # A row-major file (an older writer) loads transposed into the same orientation.
    rowmajor = json.dumps({"p_goal": [[grid.p_goal[cx][cy] for cx in range(COLS)] for cy in range(ROWS)],
                           "p_shot": [[grid.p_shot[cx][cy] for cx in range(COLS)] for cy in range(ROWS)],
                           "n": [[grid.n[cx][cy] for cx in range(COLS)] for cy in range(ROWS)]})
    assert ThreatGrid.from_json(rowmajor).p_goal == grid.p_goal


def test_follower_tracks_the_stream_exactly(league, matches, grid):
    events, meta = matches[0]
    f = Follower(league, meta, ForkPolicy(rollouts=10, horizon=8), grid=grid)
    for e in events:
        f.on_event(e)
    assert f.stats.events_seen == len(events)
    assert f.stats.mismatches == 0
    assert f.stats.forks > 0
    shots = [e for e in events if e.type == "shot" and e.subtype == "open_play"]
    forked_ids = {r["event_id"] for r in f.records}
    # Every open-play shot is forked (policy.every_shot), up to the per-match cap.
    assert all(s.event_id in forked_ids for s in shots[: min(len(shots), 5)])
    for r in f.records:
        assert r["value_model"] == "threat_grid"
        assert abs(sum(c["p_policy"] for c in r["candidates"]) - 1) < 0.01  # p_policy is rounded to 3 dp
        assert r["taken"]["taken"] and (r["best_untaken"] is None or not r["best_untaken"]["taken"])


def test_follower_without_forks_has_no_mismatches(league, matches):
    events, meta = matches[1]
    f = Follower(league, meta, ForkPolicy(max_per_match=0))
    for e in events:
        f.on_event(e)
    assert f.stats.mismatches == 0 and f.stats.forks == 0


def test_fork_service_file_mode_to_hub(league, matches, grid, tmp_path):
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        events, meta = matches[2]
        data = tmp_path / "data"
        (data / "matches").mkdir(parents=True)
        (data / "matches" / f"{meta['match_id']}.meta.json").write_text(json.dumps(meta))
        (data / "matches" / f"{meta['match_id']}.events.jsonl").write_text("\n".join(e.to_json() for e in events))
        out = Outputs(hub_url=f"http://127.0.0.1:{port}", file_path=str(tmp_path / "forks.jsonl"), quiet=True, cue_min_regret=-1.0)
        svc = ForkService(league, MetaStore(str(data)), grid, ForkPolicy(rollouts=6, horizon=6, max_per_match=5), out, hub_url=f"http://127.0.0.1:{port}")
        summary = svc.run(file_events(str(data / "matches" / meta["match_id"])), mirror_events_to_hub=True)
        out.close()
        m = summary["matches"][meta["match_id"]]
        assert m["events"] == len(events) and m["mismatches"] == 0 and m["forks"] == 5
        lines = (tmp_path / "forks.jsonl").read_text().splitlines()
        assert len(lines) == 5
        rec = json.loads(lines[0])
        assert rec["cue"]["layer"] == "ghost" and rec["record"]["match_id"] == meta["match_id"]
        assert HUB.meta["match_id"] == meta["match_id"]
        assert sum(1 for c in HUB.cues if c["layer"] == "ghost") == 5
        assert len(HUB.recent_events) > 0
    finally:
        server.shutdown()


def test_eventhouse_replay_decodes_rows(matches):
    """A fake Kusto client returning rows the way azure-kusto-data does (datetime objects, None detail)."""
    events, meta = matches[0]

    class FakeKql:
        def __init__(self):
            self.calls = 0

        def events(self, match_id, after_event_id=0):
            self.calls += 1
            rows = []
            for e in events:
                if e.event_id <= after_event_id:
                    continue
                d = e.to_dict()
                d["ts"] = datetime.fromisoformat(d["ts"])
                d["detail"] = d["detail"] or None
                d["extra_column"] = 1  # Eventhouse may carry columns the dataclass does not know
                d = {k: v for k, v in d.items() if k in Event.__dataclass_fields__ or k == "extra_column"}
                rows.append(d)
            # emulate KustoQuery.events' cleaning
            out = []
            for r in rows:
                r = {k: v for k, v in r.items() if k in Event.__dataclass_fields__}
                if hasattr(r.get("ts"), "isoformat"):
                    r["ts"] = r["ts"].isoformat()
                if r.get("detail") is None:
                    r["detail"] = {}
                out.append(Event.from_dict(r))
            return out

    kql = FakeKql()
    got = list(eventhouse_events(kql, meta["match_id"], follow=False))
    assert [e.event_id for e in got] == [e.event_id for e in events]
    assert got[-1].type == "period_end"
    assert kql.calls == 1


def test_fork_service_upgrades_cues_with_agents(league, matches, grid, tmp_path):
    """With agents on, every shown fork posts twice to the hub: template first, then the same cue_id
    with verified model words. The record file carries the explanation."""
    from http.server import ThreadingHTTPServer

    from matchlab.agents import FakeModel

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        HUB.broadcast("reset", None)
        events, meta = matches[1]
        data = tmp_path / "data"
        (data / "matches").mkdir(parents=True)
        (data / "matches" / f"{meta['match_id']}.meta.json").write_text(json.dumps(meta))
        metas = MetaStore(str(data))
        out = Outputs(hub_url=f"http://127.0.0.1:{port}", file_path=str(tmp_path / "forks.jsonl"), quiet=True, cue_min_regret=-1.0,
                      model=FakeModel(), metas=metas, sync_agents=True)
        svc = ForkService(league, metas, grid, ForkPolicy(rollouts=6, horizon=6, max_per_match=3), out, hub_url=f"http://127.0.0.1:{port}")
        svc.run(iter(events))
        out.close()
        assert out.verified == 3 and out.fallbacks == 0
        ghosts = [c for c in HUB.cues if c["layer"] == "ghost"]
        assert len(ghosts) == 6, "template post plus upgrade post per fork"
        ids = {c["cue_id"] for c in ghosts}
        assert len(ids) == 3
        upgraded = [c for c in ghosts if c["geometry"].get("update")]
        assert len(upgraded) == 3 and all(c["geometry"]["text_source"] == "model" for c in upgraded)
        rows = [json.loads(l) for l in (tmp_path / "forks.jsonl").read_text().splitlines()]
        assert len(rows) == 3 and all("explanation" in r["record"] for r in rows)
        assert all(r["record"]["explanation"]["verified"]["fr"] for r in rows)
    finally:
        server.shutdown()
