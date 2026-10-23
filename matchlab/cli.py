"""matchlab: generate a fictional league, simulate matches, replay them as a live stream,
and fork a decision to see what the alternatives were worth."""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

from .export import export_all
from .fork import action_events, fork, rebuild_to_event
from .league import League, make_league
from .schema import JSON_SCHEMA, Event
from .sim import Simulator
from .sinks import make_sink, read_jsonl
from .stats import summarize


def _load_league(path: str) -> League:
    with open(path, encoding="utf-8") as f:
        return League.from_json(f.read())


def cmd_league(a: argparse.Namespace) -> int:
    league = make_league(a.seed)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(league.to_json())
    print(f"wrote {a.out}: {len(league.teams)} clubs, {sum(len(t.players) for t in league.teams)} players")
    return 0


def cmd_generate(a: argparse.Namespace) -> int:
    league = _load_league(a.league)
    rng = random.Random(a.seed)
    os.makedirs(os.path.join(a.out, "matches"), exist_ok=True)
    os.makedirs(os.path.join(a.out, "truth"), exist_ok=True)
    kickoff = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    ids = [t.team_id for t in league.teams]
    for i in range(a.matches):
        home, away = rng.sample(ids, 2)
        seed = a.seed * 1000 + i
        sim = Simulator(league, home, away, seed, kickoff=kickoff + timedelta(hours=3 * i))
        events = sim.run()
        meta = sim.metadata()
        mid = sim.match_id
        with open(os.path.join(a.out, "matches", f"{mid}.events.jsonl"), "w", encoding="utf-8") as f:
            for e in events:
                f.write(e.to_json() + "\n")
        with open(os.path.join(a.out, "matches", f"{mid}.meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)
        with open(os.path.join(a.out, "truth", f"{mid}.truth.json"), "w", encoding="utf-8") as f:
            f.write(sim.truth.to_json())
        last = events[-1]
        print(f"{mid}: {sim.home.name} {last.score_home}-{last.score_away} {sim.away.name}  ({len(events)} events)")
    with open(os.path.join(a.out, "event.schema.json"), "w", encoding="utf-8") as f:
        json.dump(JSON_SCHEMA, f, indent=1)
    return 0


def _load_match(prefix: str) -> tuple[list[Event], dict]:
    """prefix = path without .events.jsonl / .meta.json"""
    events = [Event.from_dict(d) for d in read_jsonl(prefix + ".events.jsonl")]
    with open(prefix + ".meta.json", encoding="utf-8") as f:
        meta = json.load(f)
    return events, meta


def cmd_stats(a: argparse.Namespace) -> int:
    events, meta = _load_match(a.match)
    print(json.dumps(summarize(events, meta), indent=1))
    return 0


def cmd_stream(a: argparse.Namespace) -> int:
    sink = make_sink(a.sink)
    rows = list(read_jsonl(a.match + ".events.jsonl"))
    start_wall = time.monotonic()
    first_clock = rows[0]["clock_s"]
    batch: list[dict] = []
    sent = 0
    try:
        for d in rows:
            due = (d["clock_s"] - first_clock) / a.speed
            # Period 2 starts at 2700 on the match clock; no half-time wait in a replay.
            while time.monotonic() - start_wall < due:
                time.sleep(min(0.05, due - (time.monotonic() - start_wall)))
            if a.live_ts:
                d["ts"] = datetime.now(timezone.utc).isoformat()
            batch.append(d)
            if len(batch) >= a.batch:
                sink.send(batch)
                sent += len(batch)
                batch = []
        if batch:
            sink.send(batch)
            sent += len(batch)
    finally:
        sink.close()
    print(f"streamed {sent} events at {a.speed}x", file=sys.stderr)
    return 0


def cmd_export(a: argparse.Namespace) -> int:
    counts = export_all(a.data, a.out)
    for name, n in counts.items():
        print(f"{name}.jsonl: {n} rows")
    return 0


def cmd_fork(a: argparse.Namespace) -> int:
    league = _load_league(a.league)
    events, meta = _load_match(a.match)
    if a.event_id is None:
        shots = [e for e in action_events(events) if e.type == "shot"]
        if not shots:
            print("no open-play shots to fork", file=sys.stderr)
            return 1
        target = max(shots, key=lambda e: (e.xg or 0))
        print(f"no --event-id given; forking the highest-xG open-play shot, event {target.event_id}", file=sys.stderr)
        a.event_id = target.event_id
    sim, ok = rebuild_to_event(league, meta, a.event_id)
    if not ok:
        print(f"event {a.event_id} is not forkable (restart, tick, or not the first event of its action)", file=sys.stderr)
        return 1
    record = fork(sim, n_rollouts=a.rollouts, horizon=a.horizon)
    actual = next(e for e in events if e.event_id == a.event_id)
    record["actual_event"] = actual.to_dict()
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=1)
    t = record
    print(f"{t['match_id']} minute {t['minute']} {t['player_name']} at ({t['x']}, {t['y']})"
          f"{' under pressure' if t['under_pressure'] else ''}, score {t['score']['home']}-{t['score']['away']}")
    print(f"{'action':<38}{'policy':>8}{'P(goal for)':>13}{'P(goal ag)':>12}{'xG for':>8}{'value':>8}")
    for r in t["candidates"]:
        label = r["type"] + (f" {r['subtype']}" if r.get("subtype") else "")
        if r.get("receiver_name"):
            label += f" to {r['receiver_name']}"
        mark = " *" if r["taken"] else ""
        print(f"{(label + mark)[:38]:<38}{r['p_policy']:>8.3f}{r['p_goal_for']:>13.3f}{r['p_goal_against']:>12.3f}{r['xg_for']:>8.3f}{r['value']:>8.3f}")
    print(f"regret (best untaken minus taken): {t['regret']:+.3f} goals per {t['horizon_events']} events, {t['rollouts']} rollouts each")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="matchlab", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("league", help="write a fictional league")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--out", default="data/league.json")
    s.set_defaults(fn=cmd_league)

    s = sub.add_parser("generate", help="simulate matches into data/matches and data/truth")
    s.add_argument("--league", default="data/league.json")
    s.add_argument("--matches", type=int, default=10)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--out", default="data")
    s.set_defaults(fn=cmd_generate)

    s = sub.add_parser("stats", help="summarise one match")
    s.add_argument("match", help="path prefix, e.g. data/matches/NBU-HAR-1000")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("stream", help="replay a match as a live stream")
    s.add_argument("match", help="path prefix, e.g. data/matches/NBU-HAR-1000")
    s.add_argument("--speed", type=float, default=10.0, help="match seconds per wall second")
    s.add_argument("--sink", default="stdout", help="stdout | file:<path> | http(s)://url | eventhub")
    s.add_argument("--batch", type=int, default=1)
    s.add_argument("--live-ts", action="store_true", help="rewrite ts to now")
    s.set_defaults(fn=cmd_stream)

    s = sub.add_parser("export", help="flatten league, metadata and truth into JSONL for Eventhouse and Lakehouse")
    s.add_argument("--data", default="data")
    s.add_argument("--out", default="data/export")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("fork", help="rewind to a decision and price the alternatives")
    s.add_argument("match", help="path prefix, e.g. data/matches/NBU-HAR-1000")
    s.add_argument("--league", default="data/league.json")
    s.add_argument("--event-id", type=int, default=None)
    s.add_argument("--rollouts", type=int, default=200)
    s.add_argument("--horizon", type=int, default=20)
    s.add_argument("--out", default=None, help="write the fork record as JSON")
    s.set_defaults(fn=cmd_fork)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
