"""The Otherwise fork service: follow a live match, fork the decisions that matter, publish the
ghosts.

    matchlab forkd --source eventhub                      # live: tail the Eventstream's Event Hub
    matchlab forkd --source eventhouse --match RAV-IRO-1000   # replay a match already in Eventhouse
    matchlab forkd --source file --match data/matches/RAV-IRO-1000   # offline, for tests and demos

Inputs deliver events in stream order; one Follower per match_id keeps a deterministic copy of the
match in step and forks just before each decision the policy cares about. Outputs fan out every
fork record: to the hub (so the renderer draws the ghost), to a JSONL file, and to Eventhouse's
ForkRecords table by queued ingestion.

Match metadata (line-ups) is needed before the first event. The service looks for it in
data/matches/<match_id>.meta.json, then in the Eventhouse Matches and Lineups tables when a KQL
connection is configured.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Optional

from .cues import Cue
from .follow import Follower, ForkPolicy
from .league import League
from .schema import Event
from .sinks import HttpSink, read_jsonl
from .threat import ThreatGrid


# ------------------------------------------------------------------ metadata lookup

class MetaStore:
    def __init__(self, data_dir: str = "data", kql: Optional["KustoQuery"] = None):
        self.data_dir = data_dir
        self.kql = kql
        self.cache: dict[str, dict[str, Any]] = {}

    def get(self, match_id: str) -> Optional[dict[str, Any]]:
        if match_id in self.cache:
            return self.cache[match_id]
        path = os.path.join(self.data_dir, "matches", f"{match_id}.meta.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.cache[match_id] = json.load(f)
            return self.cache[match_id]
        if self.kql is not None:
            meta = self.kql.match_meta(match_id)
            if meta:
                self.cache[match_id] = meta
                return meta
        return None


# ------------------------------------------------------------------ Eventhouse access

class KustoQuery:
    """Read from Eventhouse. Needs `pip install azure-kusto-data` and a login: `az login` for the
    CLI credential, or AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET for an app."""

    def __init__(self, query_uri: str, database: str):
        from azure.kusto.data import KustoClient, KustoConnectionStringBuilder  # type: ignore

        if os.environ.get("AZURE_CLIENT_SECRET"):
            kcsb = KustoConnectionStringBuilder.with_aad_application_key_authentication(
                query_uri, os.environ["AZURE_CLIENT_ID"], os.environ["AZURE_CLIENT_SECRET"], os.environ["AZURE_TENANT_ID"])
        else:
            kcsb = KustoConnectionStringBuilder.with_az_cli_authentication(query_uri)
        self.client = KustoClient(kcsb)
        self.database = database

    def rows(self, query: str) -> list[dict[str, Any]]:
        resp = self.client.execute(self.database, query)
        table = resp.primary_results[0]
        cols = [c.column_name for c in table.columns]
        return [dict(zip(cols, r)) for r in table.rows]

    def events(self, match_id: str, after_event_id: int = 0) -> list[Event]:
        q = f"MatchEvents | where match_id == '{match_id}' and event_id > {after_event_id} | order by event_id asc"
        out = []
        for r in self.rows(q):
            r = {k: v for k, v in r.items() if k in Event.__dataclass_fields__}
            if hasattr(r.get("ts"), "isoformat"):
                r["ts"] = r["ts"].isoformat()
            if r.get("detail") is None:
                r["detail"] = {}
            out.append(Event.from_dict(r))
        return out

    def match_meta(self, match_id: str) -> Optional[dict[str, Any]]:
        m = self.rows(f"Matches | where match_id == '{match_id}'")
        if not m:
            return None
        m = m[0]
        lineups = self.rows(f"Lineups | where match_id == '{match_id}'")
        def side(s: str, tid_key: str, name_key: str, short_key: str, form_key: str) -> dict[str, Any]:
            return {"team_id": m[tid_key], "name": m[name_key], "short_name": m[short_key], "formation": m[form_key],
                    "lineup": [{"player_id": l["player_id"], "name": l["name"], "shirt": l["shirt"], "role": l["role"],
                                "x": l["x"], "y": l["y"], "starter": l["starter"]} for l in lineups if l["side"] == s]}
        kickoff = m["kickoff"].isoformat() if hasattr(m["kickoff"], "isoformat") else str(m["kickoff"])
        return {"match_id": match_id, "seed": int(m["seed"]), "league_seed": int(m["league_seed"]), "kickoff": kickoff,
                "venue": m["venue"], "home": side("home", "home_team_id", "home_name", "home_short", "home_formation"),
                "away": side("away", "away_team_id", "away_name", "away_short", "away_formation")}


class KustoIngest:
    """Queued ingestion into ForkRecords and Cues. Needs `pip install azure-kusto-ingest`."""

    def __init__(self, ingest_uri: str, database: str):
        from azure.kusto.data import KustoConnectionStringBuilder  # type: ignore
        from azure.kusto.data.data_format import DataFormat  # type: ignore
        from azure.kusto.ingest import IngestionProperties, QueuedIngestClient  # type: ignore

        if os.environ.get("AZURE_CLIENT_SECRET"):
            kcsb = KustoConnectionStringBuilder.with_aad_application_key_authentication(
                ingest_uri, os.environ["AZURE_CLIENT_ID"], os.environ["AZURE_CLIENT_SECRET"], os.environ["AZURE_TENANT_ID"])
        else:
            kcsb = KustoConnectionStringBuilder.with_az_cli_authentication(ingest_uri)
        self.client = QueuedIngestClient(kcsb)
        self.props = lambda table: IngestionProperties(database=database, table=table, data_format=DataFormat.MULTIJSON)

    def send(self, table: str, rows: list[dict[str, Any]]) -> None:
        import io
        from azure.kusto.ingest import StreamDescriptor  # type: ignore

        payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8")
        self.client.ingest_from_stream(StreamDescriptor(io.BytesIO(payload), size=len(payload)), ingestion_properties=self.props(table))


# ------------------------------------------------------------------ inputs

def file_events(prefix: str) -> Iterator[Event]:
    for d in read_jsonl(prefix + ".events.jsonl"):
        yield Event.from_dict(d)


def eventhouse_events(kql: KustoQuery, match_id: str, follow: bool = False, poll_s: float = 2.0) -> Iterator[Event]:
    """Replay a match from Eventhouse; with follow=True keep polling for new rows (a live match
    arriving through the Eventstream)."""
    last = 0
    idle = 0
    while True:
        batch = kql.events(match_id, after_event_id=last)
        for e in batch:
            last = e.event_id
            yield e
        if batch and batch[-1].type == "period_end" and batch[-1].detail.get("period") == 2:
            return
        if not follow:
            return
        idle = idle + 1 if not batch else 0
        if idle > 300:  # ten minutes without a row
            return
        time.sleep(poll_s)


def eventhub_events(connection_string: Optional[str] = None, name: Optional[str] = None, consumer_group: str = "$Default",
                    from_start: bool = False) -> Iterator[Event]:
    """Tail the Eventstream's Event Hub. Uses the same connection string as `matchlab stream`."""
    import queue
    from azure.eventhub import EventHubConsumerClient  # type: ignore

    cs = connection_string or os.environ["EVENTHUB_CONNECTION_STRING"]
    nm = name or os.environ["EVENTHUB_NAME"]
    q: "queue.Queue[Optional[Event]]" = queue.Queue(maxsize=10000)

    def on_event(partition_context: Any, event: Any) -> None:
        if event is None:
            return
        body = event.body_as_str()
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows = d if isinstance(d, list) else [d]
            for r in rows:
                try:
                    q.put(Event.from_dict(r))
                except TypeError:
                    continue
        partition_context.update_checkpoint(event)

    client = EventHubConsumerClient.from_connection_string(cs, consumer_group=consumer_group, eventhub_name=nm)

    def run() -> None:
        with client:
            client.receive(on_event=on_event, starting_position="-1" if from_start else "@latest")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while True:
        ev = q.get()
        if ev is None:
            return
        yield ev


# ------------------------------------------------------------------ the service

@dataclass
class Outputs:
    hub_url: Optional[str] = None
    file_path: Optional[str] = None
    ingest: Optional[KustoIngest] = None
    quiet: bool = False
    cue_min_regret: float = 0.03
    model: Any = None                 # a matchlab.agents ModelClient; None keeps template text
    metas: Any = None                 # MetaStore, needed to build a Pipeline per match
    sync_agents: bool = False         # True: wait for the model before returning (tests, explain)
    _file: Any = None
    _pipelines: dict = field(default_factory=dict)
    _threads: list = field(default_factory=list)
    _lock: Any = field(default_factory=threading.Lock)
    sent: int = 0
    posted: int = 0
    verified: int = 0
    fallbacks: int = 0

    def __post_init__(self) -> None:
        if self.file_path:
            self._file = open(self.file_path, "a", encoding="utf-8")

    def _pipeline(self, match_id: str) -> Any:
        if self.model is None or self.metas is None:
            return None
        if match_id not in self._pipelines:
            from .agents import Pipeline
            meta = self.metas.get(match_id)
            self._pipelines[match_id] = Pipeline(self.model, meta) if meta else None
        return self._pipelines[match_id]

    def on_fork(self, record: dict[str, Any], cue: Cue) -> None:
        """Publish the ghost now with template words; if the agents are on, upgrade the same cue in
        place when the model's words verify. The match never waits for the model."""
        self.sent += 1
        shown = record["regret"] >= self.cue_min_regret or record["taken"]["type"] == "shot"
        self._publish(record, cue, first=True, shown=shown)
        pipe = self._pipeline(record["match_id"])
        if pipe is not None and shown:
            def upgrade() -> None:
                new_cue, ex = pipe.cue(record)
                record["explanation"] = {"text": ex.text, "text_analyst": ex.text_analyst, "verified": ex.verified,
                                         "problems": ex.problems, "model_calls": ex.model_calls, "seconds": ex.seconds}
                if ex.fallback:
                    self.fallbacks += 1
                else:
                    self.verified += 1
                if not self.quiet:
                    tag = "verified" if not ex.fallback else f"FALLBACK {ex.problems}"
                    print(f"         {tag} ({ex.seconds:.1f}s): {new_cue.text['en']}", file=sys.stderr)
                if any(ex.verified.values()):
                    new_cue.geometry["update"] = True
                    self._publish(record, new_cue, first=False, shown=True)
                self._write_record(record, new_cue)
            if self.sync_agents:
                upgrade()
            else:
                t = threading.Thread(target=upgrade, daemon=True)
                t.start()
                self._threads.append(t)
        else:
            self._write_record(record, cue)

    def _write_record(self, record: dict[str, Any], cue: Cue) -> None:
        if self._file:
            with self._lock:
                self._file.write(json.dumps({"record": record, "cue": cue.to_dict()}, ensure_ascii=False) + "\n")
                self._file.flush()
        if self.ingest:
            try:
                row = {k: v for k, v in record.items() if k != "fork_ms"}
                self.ingest.send("ForkRecords", [row])
            except Exception as e:
                print(f"  eventhouse ingest failed: {e}", file=sys.stderr)

    def _publish(self, record: dict[str, Any], cue: Cue, first: bool, shown: bool) -> None:
        if first and not self.quiet:
            t, b = record["taken"], record["best_untaken"]
            alt = f"{b['type']}{' to ' + b['receiver_name'] if b and b.get('receiver_name') else ''}" if b else "-"
            print(f"  fork {self.sent:>3}  min {record['minute']:>2}  {record['player_name']:<20} {t['type']:<9} regret {record['regret']:+.3f}  best: {alt:<28} {record.get('fork_ms', 0)} ms", file=sys.stderr)
        if self.hub_url and shown:
            self.posted += 1
            try:
                HttpSink(self.hub_url.rstrip("/") + "/cues").send([cue.to_dict()])
            except Exception as e:
                print(f"  hub post failed: {e}", file=sys.stderr)
        if self.ingest and shown:
            try:
                c = cue.to_dict()
                self.ingest.send("Cues", [{"cue_id": c["cue_id"], "match_id": c["match_id"], "t_start_s": c["t_start_s"], "duration_s": c["duration_s"],
                                           "layer": c["layer"], "geometry": c["geometry"], "text": c["text"], "priority": c["priority"],
                                           "modes": c["modes"], "source": c["source"], "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}])
            except Exception as e:
                print(f"  eventhouse ingest failed: {e}", file=sys.stderr)

    def wait(self, timeout: float = 60.0) -> None:
        """Let in-flight model calls finish (end of a match)."""
        for t in self._threads:
            t.join(timeout)
        self._threads = [t for t in self._threads if t.is_alive()]
        if not self.quiet:
            t, b = record["taken"], record["best_untaken"]
            alt = f"{b['type']}{' to ' + b['receiver_name'] if b and b.get('receiver_name') else ''}" if b else "-"
            print(f"  fork {self.sent:>3}  min {record['minute']:>2}  {record['player_name']:<20} {t['type']:<9} regret {record['regret']:+.3f}  best: {alt:<28} {record.get('fork_ms', 0)} ms", file=sys.stderr)

    def close(self) -> None:
        self.wait()
        if self._file:
            self._file.close()


class ForkService:
    def __init__(self, league: League, metas: MetaStore, grid: Optional[ThreatGrid], policy: ForkPolicy, outputs: Outputs,
                 hub_url: Optional[str] = None):
        self.league = league
        self.metas = metas
        self.grid = grid
        self.policy = policy
        self.outputs = outputs
        self.hub_url = hub_url
        self.followers: dict[str, Follower] = {}
        self.skipped: set[str] = set()

    def follower(self, match_id: str) -> Optional[Follower]:
        if match_id in self.followers:
            return self.followers[match_id]
        if match_id in self.skipped:
            return None
        meta = self.metas.get(match_id)
        if meta is None:
            print(f"no metadata for {match_id}; skipping its events (generate it, or load Matches and Lineups)", file=sys.stderr)
            self.skipped.add(match_id)
            return None
        print(f"following {match_id}: {meta['home']['name']} v {meta['away']['name']}", file=sys.stderr)
        if self.hub_url:
            try:
                import urllib.request
                req = urllib.request.Request(self.hub_url.rstrip("/") + "/meta", data=json.dumps(meta).encode("utf-8"),
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                pass
        f = Follower(self.league, meta, self.policy, grid=self.grid, on_fork=self.outputs.on_fork)
        self.followers[match_id] = f
        return f

    def run(self, events: Iterable[Event], mirror_events_to_hub: bool = False) -> dict[str, Any]:
        t0 = time.time()
        n = 0
        for ev in events:
            n += 1
            f = self.follower(ev.match_id)
            if f is None:
                continue
            if mirror_events_to_hub and self.hub_url:
                try:
                    HttpSink(self.hub_url.rstrip("/") + "/events").send([ev.to_dict()])
                except Exception:
                    pass
            f.on_event(ev)
        summary = {"events": n, "seconds": round(time.time() - t0, 1), "matches": {}}
        for mid, f in self.followers.items():
            summary["matches"][mid] = {"events": f.stats.events_seen, "mismatches": f.stats.mismatches, "forks": f.stats.forks,
                                       "worth_it": sum(1 for r in f.records if r["regret"] >= 0.03),
                                       "ms_per_fork": round(f.stats.fork_seconds / max(1, f.stats.forks) * 1000)}
        return summary


def load_grid(path: Optional[str], data_dir: str) -> Optional[ThreatGrid]:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return ThreatGrid.from_json(f.read())
    files = sorted(glob.glob(os.path.join(data_dir, "matches", "*.events.jsonl")))
    if not files:
        return None
    grid = ThreatGrid.learn([Event.from_dict(d) for d in read_jsonl(p)] for p in files)
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(grid.to_json())
    return grid
