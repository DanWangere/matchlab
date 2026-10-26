"""Replay for the viewer: the match as it was shown, from Cosmos DB.

The fork service writes three kinds of document into one container partitioned by match_id while a
match is live: the metadata (``meta``), the events chunked per match minute (``events-<period>-<minute>``)
and every cue it published (``cue-<cue_id>``, upserted again when the agents upgraded its words, so the
stored cue is the final one). A replay reads that partition back and paces it out again, through the
same hub and the same renderer, at any speed. The baseline cues (goals, cards, the momentum dial) are
recomputed from the stored events, since they are deterministic.

Why Cosmos DB and not the Eventhouse: a replay is a point read of one partition from a viewer's
session, not an analytical query; the upsert-by-id matches how a cue is upgraded in place; and viewer
state (mode, language, favourite player) has the same home. The Eventhouse keeps the analytics.

Sign-in: ``COSMOS_ENDPOINT`` plus either ``az login`` / a managed identity holding the Cosmos DB
Built-in Data Contributor role, or ``COSMOS_KEY``. Database and container are created by the portal,
the CLI or infra/cosmos.bicep, not here: data-plane roles cannot create them.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Any, Callable, Iterable, Optional

from .cues import cues_from_events
from .schema import Event

GHOST_LEAD_S = 3.5   # a live ghost reached the screen about this long before its cue's t_start_s


def doc_id(kind: str, *parts: Any) -> str:
    raw = "-".join([kind, *[str(p) for p in parts]])
    return re.sub(r"[/\\?#]", "_", raw)


class CosmosStore:
    """The replay store. `container` may be injected (tests, or a client built elsewhere)."""

    def __init__(self, endpoint: Optional[str] = None, database: Optional[str] = None, container_name: Optional[str] = None,
                 key: Optional[str] = None, container: Any = None):
        self.endpoint = endpoint or os.environ.get("COSMOS_ENDPOINT")
        self.database = database or os.environ.get("COSMOS_DATABASE", "matchlab")
        self.container_name = container_name or os.environ.get("COSMOS_CONTAINER", "matches")
        self.key = key or os.environ.get("COSMOS_KEY")
        self._container = container
        self._lock = threading.Lock()
        self.writes = 0

    @classmethod
    def from_env(cls) -> Optional["CosmosStore"]:
        return cls() if os.environ.get("COSMOS_ENDPOINT") else None

    @property
    def container(self) -> Any:
        if self._container is None:
            if not self.endpoint:
                raise SystemExit("set COSMOS_ENDPOINT (the account's URI) to use the replay store")
            from azure.cosmos import CosmosClient  # type: ignore

            if self.key:
                credential: Any = self.key
            else:
                from azure.identity import DefaultAzureCredential  # type: ignore

                credential = DefaultAzureCredential()
            client = CosmosClient(self.endpoint, credential=credential)
            self._container = client.get_database_client(self.database).get_container_client(self.container_name)
        return self._container

    # ---- writes

    def _upsert(self, doc: dict[str, Any]) -> None:
        with self._lock:
            self.container.upsert_item(doc)
            self.writes += 1

    def put_meta(self, meta: dict[str, Any], **extra: Any) -> None:
        self._upsert({"id": "meta", "type": "meta", "match_id": meta["match_id"], "meta": meta, **extra})

    def put_cue(self, cue: dict[str, Any]) -> None:
        self._upsert({"id": doc_id("cue", cue["cue_id"]), "type": "cue", "match_id": cue["match_id"], "cue": cue})

    def put_events(self, match_id: str, period: int, minute: int, rows: list[dict[str, Any]]) -> None:
        self._upsert({"id": doc_id("events", period, f"{minute:03d}"), "type": "events", "match_id": match_id,
                      "period": period, "minute": minute, "rows": rows})

    # ---- reads

    def _query(self, query: str, params: list[dict[str, Any]], cross: bool = False) -> list[dict[str, Any]]:
        return list(self.container.query_items(query=query, parameters=params, enable_cross_partition_query=cross))

    def matches(self) -> list[dict[str, Any]]:
        """One row per stored match, newest kick-off first."""
        rows = self._query("SELECT c.match_id, c.meta.home.short_name AS home, c.meta.away.short_name AS away, "
                           "c.meta.home.name AS home_name, c.meta.away.name AS away_name, c.meta.kickoff AS kickoff, "
                           "c.score AS score, c.ended AS ended FROM c WHERE c.type = 'meta'", [], cross=True)
        return sorted(rows, key=lambda r: r.get("kickoff") or "", reverse=True)

    def bundle(self, match_id: str) -> Optional[dict[str, Any]]:
        docs = self._query("SELECT * FROM c WHERE c.match_id = @m", [{"name": "@m", "value": match_id}])
        meta = next((d["meta"] for d in docs if d.get("type") == "meta"), None)
        if meta is None:
            return None
        events = [Event.from_dict(r) for d in docs if d.get("type") == "events" for r in d["rows"]]
        events.sort(key=lambda e: e.event_id)
        cues = [d["cue"] for d in docs if d.get("type") == "cue"]
        cues.sort(key=lambda c: (c["period"], c["t_start_s"]))
        return {"meta": meta, "events": events, "cues": cues}


class EventChunker:
    """Buffers a match's events per minute and writes each minute as one document. Flushes on the
    minute change, at the end of each period, and on close; stamps the final score on the metadata."""

    def __init__(self, store: CosmosStore):
        self.store = store
        self.buf: dict[str, list[dict[str, Any]]] = {}
        self.key: dict[str, tuple[int, int]] = {}
        self.last: dict[str, Event] = {}

    def on_event(self, ev: Event) -> None:
        k = (ev.period, int(ev.clock_s // 60))
        mid = ev.match_id
        if mid in self.key and self.key[mid] != k:
            self.flush(mid)
        self.key[mid] = k
        self.buf.setdefault(mid, []).append(ev.to_dict())
        self.last[mid] = ev
        if ev.type == "period_end":
            self.flush(mid)
            if ev.detail.get("period") == 2:
                self.finish(mid)

    def flush(self, match_id: str) -> None:
        rows = self.buf.pop(match_id, [])
        if rows and match_id in self.key:
            period, minute = self.key[match_id]
            try:
                self.store.put_events(match_id, period, minute, rows)
            except Exception as e:  # the match never waits for the store
                print(f"  replay store write failed: {e}", file=sys.stderr)

    def finish(self, match_id: str, meta: Optional[dict[str, Any]] = None) -> None:
        last = self.last.get(match_id)
        if last is None:
            return
        try:
            if meta is None:
                doc = self.store.container.read_item(item="meta", partition_key=match_id)
                meta = doc["meta"]
            self.store.put_meta(meta, score={"home": last.score_home, "away": last.score_away}, ended=True,
                                events=last.event_id + 1)
        except Exception as e:
            print(f"  replay store write failed: {e}", file=sys.stderr)

    def close(self) -> None:
        for mid in list(self.buf):
            self.flush(mid)


# ------------------------------------------------------------------ pacing a stored match out again

def replay_cues(bundle: dict[str, Any], baseline: bool = True) -> list[dict[str, Any]]:
    """Stored cues plus, when asked, the deterministic baseline recomputed from the events."""
    cues = list(bundle["cues"])
    have = {c["cue_id"] for c in cues}
    if baseline:
        events, meta = bundle["events"], bundle["meta"]
        for c in cues_from_events(events, meta):   # includes the momentum dial
            if c.cue_id not in have:
                cues.append(c.to_dict())
    return cues


def due_at(cue: dict[str, Any]) -> float:
    lead = GHOST_LEAD_S if cue["layer"] == "ghost" else 0.0
    return cue["t_start_s"] - lead


def pace(bundle: dict[str, Any], emit_meta: Callable[[dict[str, Any]], None], emit_event: Callable[[dict[str, Any]], None],
         emit_cue: Callable[[dict[str, Any]], None], speed: float = 10.0, baseline: bool = True,
         sleep: Callable[[float], None] = time.sleep, stop: Optional[threading.Event] = None,
         max_gap_s: float = 3.0) -> dict[str, int]:
    """Emit metadata, then events and cues on the match clock divided by `speed`. Cues go out when the
    clock reaches their due time, so a ghost lands where it did live. Returns counts."""
    events: list[Event] = bundle["events"]
    cues = sorted(replay_cues(bundle, baseline), key=due_at)
    emit_meta(bundle["meta"])
    sent_events = sent_cues = 0
    ci = 0
    clock = events[0].clock_s if events else 0.0
    for ev in events:
        if stop is not None and stop.is_set():
            break
        gap = max(0.0, ev.clock_s - clock) / speed
        if gap > 0:
            sleep(min(gap, max_gap_s))
        clock = ev.clock_s
        while ci < len(cues) and due_at(cues[ci]) <= clock:
            emit_cue(cues[ci])
            sent_cues += 1
            ci += 1
        emit_event(ev.to_dict())
        sent_events += 1
    while ci < len(cues) and (stop is None or not stop.is_set()):
        emit_cue(cues[ci])
        sent_cues += 1
        ci += 1
    return {"events": sent_events, "cues": sent_cues}


def file_bundle(prefix: str, forks_path: Optional[str] = None) -> dict[str, Any]:
    """A bundle from data/matches/<id> files plus, optionally, the ghosts in a forkd record file."""
    import json

    from .sinks import read_jsonl

    with open(prefix + ".meta.json", encoding="utf-8") as f:
        meta = json.load(f)
    events = [Event.from_dict(d) for d in read_jsonl(prefix + ".events.jsonl")]
    cues: list[dict[str, Any]] = []
    if forks_path and os.path.exists(forks_path):
        seen: dict[str, dict[str, Any]] = {}
        for row in read_jsonl(forks_path):
            c = row.get("cue")
            if c and c.get("match_id") == meta["match_id"]:
                seen[c["cue_id"]] = c   # the last line for a cue_id carries the upgraded words
        cues = list(seen.values())
    return {"meta": meta, "events": events, "cues": cues}
