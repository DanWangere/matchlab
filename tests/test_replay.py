"""Replay: the fork service records a match in the store as it runs; a replay reads it back and paces it
out through the hub with the ghosts where they were. A fake container stands in for Cosmos DB."""
import json
import re
import threading
from http.server import ThreadingHTTPServer

import pytest

from matchlab.follow import ForkPolicy
from matchlab.forkd import ForkService, MetaStore, Outputs
from matchlab.league import make_league
from matchlab.replay import CosmosStore, due_at, file_bundle, pace
from matchlab.serve import HUB, Handler
from matchlab.sim import Simulator
from matchlab.threat import ThreatGrid


class FakeContainer:
    """Enough of azure.cosmos ContainerProxy for the store: upsert_item, read_item, query_items with the
    two query shapes the store uses."""

    def __init__(self):
        self.docs = {}

    def upsert_item(self, doc):
        assert "id" in doc and "match_id" in doc
        self.docs[(doc["match_id"], doc["id"])] = json.loads(json.dumps(doc))   # a round trip, like the wire

    def read_item(self, item, partition_key):
        return self.docs[(partition_key, item)]

    def query_items(self, query, parameters=None, enable_cross_partition_query=False):
        params = {p["name"]: p["value"] for p in (parameters or [])}
        if "c.type = 'meta'" in query:
            assert enable_cross_partition_query
            for d in self.docs.values():
                if d["type"] == "meta":
                    yield {"match_id": d["match_id"], "home": d["meta"]["home"]["short_name"], "away": d["meta"]["away"]["short_name"],
                           "home_name": d["meta"]["home"]["name"], "away_name": d["meta"]["away"]["name"],
                           "kickoff": d["meta"]["kickoff"], "score": d.get("score"), "ended": d.get("ended")}
        else:
            m = re.search(r"c.match_id = (@\w+)", query)
            for d in self.docs.values():
                if d["match_id"] == params[m.group(1)]:
                    yield d


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    """A match followed by the fork service with the store on."""
    league = make_league(7)
    sim = Simulator(league, "t03", "t09", 404)
    events, meta = sim.run(), sim.metadata()
    grid = ThreatGrid.learn([events])
    data = tmp_path_factory.mktemp("data")
    (data / "matches").mkdir()
    (data / "matches" / f"{meta['match_id']}.meta.json").write_text(json.dumps(meta))
    (data / "matches" / f"{meta['match_id']}.events.jsonl").write_text("\n".join(e.to_json() for e in events))
    fake = FakeContainer()
    store = CosmosStore(endpoint="https://fake.documents.azure.com:443/", container=fake)
    out = Outputs(file_path=str(data / "forks.jsonl"), quiet=True, cue_min_regret=-1.0, store=store)
    svc = ForkService(league, MetaStore(str(data)), grid, ForkPolicy(rollouts=6, horizon=6, max_per_match=4), out)
    summary = svc.run(iter(events))
    out.close()
    return {"events": events, "meta": meta, "fake": fake, "store": store, "summary": summary, "data": data}


def test_store_holds_the_match_as_documents(played):
    fake, meta, events = played["fake"], played["meta"], played["events"]
    mid = meta["match_id"]
    kinds = {}
    for (m, i), d in fake.docs.items():
        assert m == mid and d["match_id"] == mid
        kinds[d["type"]] = kinds.get(d["type"], 0) + 1
    assert kinds["cue"] == 4 and kinds["meta"] == 1
    assert 80 <= kinds["events"] <= 110, "about one document per match minute"
    stored = sum(len(d["rows"]) for d in fake.docs.values() if d["type"] == "events")
    assert stored == len(events)
    m = fake.docs[(mid, "meta")]
    assert m["ended"] is True and m["score"] == {"home": events[-1].score_home, "away": events[-1].score_away}
    cue = next(d["cue"] for d in fake.docs.values() if d["type"] == "cue")
    assert cue["layer"] == "ghost" and "update" not in cue["geometry"]
    assert not any("/" in i for (_, i) in fake.docs), "document ids carry no path characters"


def test_bundle_and_match_list(played):
    store, meta, events = played["store"], played["meta"], played["events"]
    b = store.bundle(meta["match_id"])
    assert [e.event_id for e in b["events"]] == [e.event_id for e in events]
    assert b["meta"]["match_id"] == meta["match_id"] and len(b["cues"]) == 4
    assert store.bundle("NO-SUCH-0000") is None
    rows = store.matches()
    assert len(rows) == 1 and rows[0]["ended"] and rows[0]["home"] == meta["home"]["short_name"]


def test_pace_emits_in_match_order_with_ghosts_ahead_of_their_moment(played):
    b = played["store"].bundle(played["meta"]["match_id"])
    out, slept = [], []
    counts = pace(b, lambda m: out.append(("meta", m)), lambda e: out.append(("event", e)), lambda c: out.append(("cue", c)),
                  speed=50.0, sleep=slept.append)
    assert out[0][0] == "meta"
    assert counts["events"] == len(b["events"]) and counts["cues"] >= 4 + 10, "stored ghosts plus the recomputed baseline"
    assert sum(slept) > 0 and max(slept) <= 3.0
    # every ghost goes out between the last event before its due time and the first event after it
    prev_clock = -1.0
    for i, (kind, payload) in enumerate(out):
        if kind == "event":
            prev_clock = payload["clock_s"]
        elif kind == "cue" and payload["layer"] == "ghost":
            next_clock = next((p["clock_s"] for k, p in out[i + 1:] if k == "event"), float("inf"))
            assert prev_clock < due_at(payload) <= next_clock
            assert payload["cue_id"] in {c["cue_id"] for c in b["cues"]}
    assert any(k == "cue" and p["layer"] == "dial" for k, p in out), "the momentum dial is part of the baseline"


def test_file_bundle_reads_forkd_records(played):
    data, meta = played["data"], played["meta"]
    b = file_bundle(str(data / "matches" / meta["match_id"]), str(data / "forks.jsonl"))
    assert len(b["events"]) == len(played["events"]) and len(b["cues"]) == 4


def test_hub_replays_from_its_store(played):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import urllib.request
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        HUB.store = played["store"]
        HUB.last_event_at = 0.0
        HUB.broadcast("reset", None)
        with urllib.request.urlopen(base + "/matches", timeout=5) as r:
            listing = json.loads(r.read())
        assert listing["store"] and listing["matches"][0]["match_id"] == played["meta"]["match_id"]
        req = urllib.request.Request(base + f"/replay/{played['meta']['match_id']}?speed=100000", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 202
        for _ in range(100):
            if HUB.replay is None:
                break
            threading.Event().wait(0.1)
        assert HUB.replay is None
        assert HUB.meta["match_id"] == played["meta"]["match_id"]
        assert len(HUB.recent_events) > 0 and any(c["layer"] == "ghost" for c in HUB.cues)
        req = urllib.request.Request(base + "/replay/NO-SUCH-0000", method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 404
    finally:
        HUB.store = None
        srv.shutdown()


def test_hub_refuses_a_replay_over_a_live_match(played):
    import time
    HUB.store = played["store"]
    HUB.replay = None
    try:
        HUB.last_event_at = time.time()
        code, body = HUB.start_replay(played["meta"]["match_id"])
        assert code == 409 and "live" in body["error"]
        code, body = HUB.start_replay(played["meta"]["match_id"], speed=100000, force=True)
        assert code == 202
        for _ in range(100):
            if HUB.replay is None:
                break
            time.sleep(0.1)
    finally:
        HUB.store = None
        HUB.last_event_at = 0.0
