"""A small local hub for the renderer: events and cues in, one server-sent-events stream out.

    matchlab play data/matches/RAV-IRO-1000 --speed 20      # serve + replay with baseline and fork cues
    matchlab serve                                           # serve only; feed it with
    matchlab stream data/matches/RAV-IRO-1000 --sink http://localhost:8080/events

Endpoints:
    GET  /            the renderer (renderer/index.html)
    GET  /stream      server-sent events: `meta`, `event`, `cue`, `reset`
    GET  /state       the current match metadata and cue list
    POST /events      a JSON array of events (what `matchlab stream --sink http://...` sends)
    POST /cues        a JSON array of cues
    POST /meta        the match metadata object (sent first so the renderer can draw line-ups)

Stdlib only, single process, fine for a demo. The production path for this hub is Azure Web PubSub
fed by Cosmos DB's change feed; the message shapes are the same.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .cues import Cue, cue_from_fork, cues_from_events
from .fork import action_events, fork, rebuild_to_event
from .league import League
from .schema import Event
from .sinks import read_jsonl

RENDERER = os.environ.get("MATCHLAB_RENDERER") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "renderer", "index.html")


class Hub:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clients: list[queue.Queue] = []
        self.meta: Optional[dict[str, Any]] = None
        self.cues: list[dict[str, Any]] = []
        self.recent_events: list[dict[str, Any]] = []
        self.last_event_at: float = 0.0       # wall clock of the last event posted from outside (a live match)
        self.store: Any = None                # a matchlab.replay.CosmosStore, when COSMOS_ENDPOINT is set
        self.replay: Optional[dict[str, Any]] = None   # the replay in progress: match_id, speed, started, stop event

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5000)
        with self.lock:
            self.clients.append(q)
            if self.meta:
                q.put(("meta", self.meta))
            for e in self.recent_events[-400:]:
                q.put(("event", e))
            for c in self.cues:
                q.put(("cue", c))
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def broadcast(self, kind: str, payload: Any) -> None:
        with self.lock:
            if kind == "meta":
                self.meta, self.cues, self.recent_events = payload, [], []
            elif kind == "cue":
                self.cues.append(payload)
            elif kind == "event":
                self.recent_events.append(payload)
                if len(self.recent_events) > 3000:
                    del self.recent_events[:1000]
                if self.replay is None:
                    self.last_event_at = time.time()
            elif kind == "reset":
                self.cues, self.recent_events = [], []
            for q in list(self.clients):
                try:
                    q.put_nowait((kind, payload))
                except queue.Full:
                    pass

    # ---- replay: a stored match, paced out again to everyone connected

    def get_store(self) -> Any:
        if self.store is None and os.environ.get("COSMOS_ENDPOINT"):
            from .replay import CosmosStore
            self.store = CosmosStore()
        return self.store

    def live(self, within_s: float = 10.0) -> bool:
        return time.time() - self.last_event_at < within_s

    def start_replay(self, match_id: str, speed: float = 10.0, baseline: bool = True, force: bool = False,
                     bundle: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
        """Returns (http status, body). 409 when a replay is running or a live match is on the hub."""
        from .replay import pace
        if self.replay is not None:
            return 409, {"error": "a replay is running", "replay": self.replay_status()}
        if self.live() and not force:
            return 409, {"error": "a live match is on the hub; add force=1 to replace it"}
        if bundle is None:
            store = self.get_store()
            if store is None:
                return 503, {"error": "no replay store: set COSMOS_ENDPOINT on the hub"}
            try:
                bundle = store.bundle(match_id)
            except Exception as e:
                return 502, {"error": f"replay store: {e}"}
            if bundle is None:
                return 404, {"error": f"no stored match {match_id}"}
        stop = threading.Event()
        self.replay = {"match_id": match_id, "speed": speed, "started": time.time(), "stop": stop,
                       "events": len(bundle["events"]), "cues": len(bundle["cues"])}

        def run() -> None:
            try:
                pace(bundle, lambda m: self.broadcast("meta", m), lambda e: self.broadcast("event", e),
                     lambda c: self.broadcast("cue", c), speed=speed, baseline=baseline, stop=stop)
            finally:
                self.replay = None

        threading.Thread(target=run, daemon=True).start()
        return 202, {"ok": True, "replay": self.replay_status()}

    def stop_replay(self) -> bool:
        r = self.replay
        if r is None:
            return False
        r["stop"].set()
        return True

    def replay_status(self) -> Optional[dict[str, Any]]:
        r = self.replay
        if r is None:
            return None
        return {k: v for k, v in r.items() if k != "stop"}


HUB = Hub()


def hub_key() -> Optional[str]:
    """When MATCHLAB_HUB_KEY is set, POSTs must carry it in X-Hub-Key. Readers stay open: the overlay
    is meant to be watched. Unset (the default, local use), anything may post."""
    return os.environ.get("MATCHLAB_HUB_KEY") or None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/index.html"):
            with open(RENDERER, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path.startswith("/state"):
            self._send(200, json.dumps({"meta": HUB.meta, "cues": HUB.cues}).encode("utf-8"))
        elif self.path.startswith("/healthz"):
            self._send(200, json.dumps({"ok": True, "viewers": len(HUB.clients), "cues": len(HUB.cues), "live": HUB.live(),
                                        "replay": HUB.replay_status()}).encode("utf-8"))
        elif self.path.startswith("/matches"):
            store = HUB.get_store()
            try:
                rows = store.matches() if store is not None else []
                self._send(200, json.dumps({"matches": rows, "store": store is not None}, default=str).encode("utf-8"))
            except Exception as e:
                self._send(502, json.dumps({"error": f"replay store: {e}"}).encode("utf-8"))
        elif self.path.startswith("/replay"):
            self._send(200, json.dumps({"replay": HUB.replay_status(), "live": HUB.live()}).encode("utf-8"))
        elif self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            q = HUB.subscribe()
            try:
                while True:
                    try:
                        kind, payload = q.get(timeout=15)
                        self.wfile.write(f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                HUB.unsubscribe(q)
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/replay"):   # replays rebroadcast stored data; no write key, but never over a live match
            from urllib.parse import parse_qs, urlparse
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            target = u.path[len("/replay"):].strip("/")
            if target == "stop":
                self._send(200, json.dumps({"stopped": HUB.stop_replay()}).encode("utf-8"))
                return
            if not target:
                self._send(400, b'{"error":"POST /replay/<match_id>?speed=10"}')
                return
            code, out = HUB.start_replay(target, speed=float(q.get("speed", 10)), baseline=q.get("baseline", "1") != "0",
                                         force=q.get("force") == "1")
            self._send(code, json.dumps(out).encode("utf-8"))
            return
        key = hub_key()
        if key and self.headers.get("X-Hub-Key") != key:
            self._send(401, b'{"error":"missing or wrong X-Hub-Key"}')
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"null")
        if self.path.startswith("/events"):
            rows = body if isinstance(body, list) else [body]
            for r in rows:
                HUB.broadcast("event", r)
            self._send(200, json.dumps({"accepted": len(rows)}).encode())
        elif self.path.startswith("/cues"):
            rows = body if isinstance(body, list) else [body]
            for r in rows:
                HUB.broadcast("cue", r)
            self._send(200, json.dumps({"accepted": len(rows)}).encode())
        elif self.path.startswith("/meta"):
            HUB.broadcast("meta", body)
            self._send(200, b'{"ok":true}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Hub-Key")
        self.end_headers()


def start_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def build_cues(events: list[Event], meta: dict[str, Any], league: Optional[League], forks: int, rollouts: int,
               horizon: int, log: Any = None) -> list[Cue]:
    cues = cues_from_events(events, meta)
    if league is not None and forks > 0:
        shots = sorted((e for e in action_events(events) if e.type == "shot"), key=lambda e: -(e.xg or 0))
        for e in shots[:forks]:
            sim, ok = rebuild_to_event(league, meta, e.event_id)
            if not ok:
                continue
            record = fork(sim, n_rollouts=rollouts, horizon=horizon)
            cues.append(cue_from_fork(record, meta))
            if log:
                log(f"fork at event {e.event_id} (minute {record['minute']}): regret {record['regret']:+.3f}")
    cues.sort(key=lambda c: (c.period, c.t_start_s, c.priority))
    return cues


def play(match_prefix: str, league_path: Optional[str], speed: float, port: int, forks: int = 3, rollouts: int = 200,
         horizon: int = 20, loop: bool = False) -> None:
    events = [Event.from_dict(d) for d in read_jsonl(match_prefix + ".events.jsonl")]
    with open(match_prefix + ".meta.json", encoding="utf-8") as f:
        meta = json.load(f)
    league = None
    if league_path and os.path.exists(league_path):
        with open(league_path, encoding="utf-8") as f:
            league = League.from_json(f.read())
    print(f"building cues for {meta['match_id']} ({forks} forks x {rollouts} rollouts)...", file=sys.stderr)
    cues = build_cues(events, meta, league, forks, rollouts, horizon, log=lambda m: print("  " + m, file=sys.stderr))
    print(f"{len(cues)} cues ready", file=sys.stderr)
    start_server(port)
    print(f"renderer at http://localhost:{port}/   (replaying at {speed}x; Ctrl+C to stop)", file=sys.stderr)
    while True:
        HUB.broadcast("meta", meta)
        pending = list(cues)
        start_wall = time.monotonic()
        first_clock = events[0].clock_s
        offset = 0.0  # wall seconds already spent before period 2 (the clock jumps back to 2700)
        last_clock = first_clock
        for ev in events:
            if ev.clock_s < last_clock:  # new period: the clock went back
                offset += (last_clock - first_clock) / speed
                first_clock = ev.clock_s
                start_wall = time.monotonic() - offset
            last_clock = ev.clock_s
            due = offset + (ev.clock_s - first_clock) / speed
            while time.monotonic() - start_wall < due:
                time.sleep(max(0.0, min(0.05, due - (time.monotonic() - start_wall))))
            HUB.broadcast("event", ev.to_dict())
            while pending and (pending[0].period < ev.period or (pending[0].period == ev.period and pending[0].t_start_s <= ev.clock_s)):
                HUB.broadcast("cue", pending[0].to_dict())
                pending.pop(0)
        for c in pending:
            HUB.broadcast("cue", c.to_dict())
        if not loop:
            break
        time.sleep(5)
    print("match over; server stays up until Ctrl+C", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def serve_only(port: int) -> None:
    start_server(port)
    print(f"renderer at http://localhost:{port}/   POST events to /events, cues to /cues, metadata to /meta", file=sys.stderr)
    if os.environ.get("COSMOS_ENDPOINT"):
        print(f"replay store: {os.environ['COSMOS_ENDPOINT']}   GET /matches, POST /replay/<match_id>?speed=10", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
