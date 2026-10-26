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
from .serve import play, serve_only
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


def cmd_play(a: argparse.Namespace) -> int:
    play(a.match, a.league, a.speed, a.port, forks=a.forks, rollouts=a.rollouts, horizon=a.horizon, loop=a.loop)
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    serve_only(a.port)
    return 0


def cmd_forkd(a: argparse.Namespace) -> int:
    from .follow import ForkPolicy
    from .forkd import ForkService, KustoIngest, KustoQuery, MetaStore, Outputs, eventhouse_events, eventhub_events, file_events, load_grid

    league = _load_league(a.league)
    kql = None
    if a.source == "eventhouse" or a.kusto_query_uri:
        uri = a.kusto_query_uri or os.environ.get("KUSTO_QUERY_URI")
        db = a.kusto_database or os.environ.get("KUSTO_DATABASE", "matchlab")
        if not uri:
            print("--kusto-query-uri (or KUSTO_QUERY_URI) is required for the eventhouse source", file=sys.stderr)
            return 2
        kql = KustoQuery(uri, db)
    ingest = None
    if a.ingest:
        iuri = a.kusto_ingest_uri or os.environ.get("KUSTO_INGEST_URI")
        db = a.kusto_database or os.environ.get("KUSTO_DATABASE", "matchlab")
        if not iuri:
            print("--kusto-ingest-uri (or KUSTO_INGEST_URI) is required with --ingest", file=sys.stderr)
            return 2
        ingest = KustoIngest(iuri, db)
    grid = None if a.value == "goals" else load_grid(a.grid, a.data)
    if a.value == "threat" and grid is None:
        print("no matches in data/ to learn the threat grid from; run `matchlab generate` or pass --value goals", file=sys.stderr)
        return 2
    policy = ForkPolicy(rollouts=a.rollouts, horizon=a.horizon, min_gap_s=a.min_gap, max_per_match=a.max_forks)
    metas = MetaStore(a.data, kql)
    model = None
    if a.agents != "off":
        from .agents import make_model
        model = make_model(a.agents)
    outputs = Outputs(hub_url=a.hub, file_path=a.out, ingest=ingest, quiet=a.quiet, cue_min_regret=a.cue_min_regret, model=model, metas=metas)
    service = ForkService(league, metas, grid, policy, outputs, hub_url=a.hub)
    if a.source == "file":
        if not a.match:
            print("--match <path prefix> is required for the file source", file=sys.stderr)
            return 2
        events = file_events(a.match)
    elif a.source == "eventhouse":
        if not a.match:
            print("--match <match_id> is required for the eventhouse source", file=sys.stderr)
            return 2
        events = eventhouse_events(kql, a.match, follow=a.follow)
    else:
        events = eventhub_events(consumer_group=a.consumer_group, from_start=a.from_start)
    print(f"forkd: source={a.source} value={a.value} rollouts={a.rollouts} horizon={a.horizon}"
          f"{' hub=' + a.hub if a.hub else ''}{' ingest=on' if ingest else ''}", file=sys.stderr)
    try:
        summary = service.run(events, mirror_events_to_hub=a.mirror)
    except KeyboardInterrupt:
        summary = {"stopped": True, "matches": {m: {"forks": f.stats.forks, "mismatches": f.stats.mismatches} for m, f in service.followers.items()}}
    finally:
        outputs.close()
    if model is not None:
        summary["agents"] = {"kind": a.agents, "verified": outputs.verified, "fallbacks": outputs.fallbacks,
                             "model_calls": getattr(model, "calls", None), "model_seconds": round(getattr(model, "seconds", 0.0), 1)}
    print(json.dumps(summary, indent=1), file=sys.stderr)
    return 0


def cmd_explain(a: argparse.Namespace) -> int:
    """Run one fork through the agent team and print what each stage produced."""
    from .agents import Pipeline, make_model

    league = _load_league(a.league)
    events, meta = _load_match(a.match)
    if a.event_id is None:
        shots = [e for e in action_events(events) if e.type == "shot"]
        a.event_id = max(shots, key=lambda e: (e.xg or 0)).event_id
    sim, ok = rebuild_to_event(league, meta, a.event_id)
    if not ok:
        print(f"event {a.event_id} is not forkable", file=sys.stderr)
        return 1
    from .forkd import load_grid
    grid = load_grid(a.grid, a.data)
    record = fork(sim, n_rollouts=a.rollouts, horizon=a.horizon, grid=grid)
    model = make_model(a.agents)
    pipe = Pipeline(model, meta)
    cue, ex = pipe.cue(record)
    print(f"{record['match_id']} minute {record['minute']}: {record['player_name']} {record['taken']['type']}, regret {record['regret']:+.3f}")
    print(f"model: {a.agents}, calls {ex.model_calls}, {ex.seconds:.1f}s, verified {ex.verified}, source {cue.geometry.get('text_source')}")
    if ex.problems:
        print("problems:", json.dumps(ex.problems, ensure_ascii=False))
    for lang in ("en", "fr", "es"):
        print(f"  [{lang}] {cue.text[lang]}")
        print(f"  [{lang}] {cue.text_analyst[lang]}")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"record": record, "cue": cue.to_dict(), "explanation": {"text": ex.text, "text_analyst": ex.text_analyst, "verified": ex.verified, "problems": ex.problems}}, f, indent=1, ensure_ascii=False)
    return 0


def cmd_recap(a: argparse.Namespace) -> int:
    """Full-time recap from a forkd record file (the --out of forkd)."""
    from .agents import Pipeline, make_model

    records = []
    with open(a.records, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                records.append(d["record"] if "record" in d else d)
    if not records:
        print("no records", file=sys.stderr)
        return 1
    match_id = records[0]["match_id"]
    with open(os.path.join(a.data, "matches", f"{match_id}.meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    last = max(records, key=lambda r: r["event_id"])
    score = last["score"]
    if a.score:
        h, s_ = a.score.split("-")
        score = {"home": int(h), "away": int(s_)}
    pipe = Pipeline(make_model(a.agents), meta)
    out = pipe.recap(records, score, top=a.top)
    print(f"{meta['home']['name']} {score['home']}-{score['away']} {meta['away']['name']}   verified {out['verified']}")
    for lang in ("en", "fr", "es"):
        print(f"\n[{lang}] {out['text'][lang]}")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1, ensure_ascii=False)
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
    if a.cue or a.post:
        from .cues import cue_from_fork
        c = cue_from_fork(record, meta)
        if a.cue:
            with open(a.cue, "w", encoding="utf-8") as f:
                f.write(c.to_json() + "\n")
        if a.post:
            from .sinks import HttpSink
            HttpSink(a.post.rstrip("/") + "/cues").send([c.to_dict()])
            print(f"posted ghost cue to {a.post}", file=sys.stderr)
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
    s.add_argument("--cue", default=None, help="also write the ghost cue as JSON")
    s.add_argument("--post", default=None, help="also post the ghost cue to a running hub, e.g. http://localhost:8080")
    s.set_defaults(fn=cmd_fork)

    s = sub.add_parser("forkd", help="the Otherwise fork service: follow a stream, fork the decisions that matter, publish ghosts")
    s.add_argument("--source", choices=["eventhub", "eventhouse", "file"], default="eventhub")
    s.add_argument("--match", default=None, help="file: path prefix; eventhouse: match_id")
    s.add_argument("--league", default="data/league.json")
    s.add_argument("--data", default="data", help="where match metadata and the threat grid's matches live")
    s.add_argument("--value", choices=["threat", "goals"], default="threat")
    s.add_argument("--grid", default="data/threat_grid.json", help="cache for the learned threat grid")
    s.add_argument("--rollouts", type=int, default=120)
    s.add_argument("--horizon", type=int, default=20)
    s.add_argument("--min-gap", type=float, default=45.0, help="seconds of match clock between non-shot forks")
    s.add_argument("--max-forks", type=int, default=25)
    s.add_argument("--cue-min-regret", type=float, default=0.03, help="post a ghost only when the untaken option beats the taken one by this much (shots always post)")
    s.add_argument("--hub", default=None, help="post ghost cues to a running hub, e.g. http://localhost:8080")
    s.add_argument("--mirror", action="store_true", help="also forward the events to the hub (one process drives the renderer)")
    s.add_argument("--out", default=None, help="append fork records and cues as JSONL")
    s.add_argument("--ingest", action="store_true", help="write ForkRecords and Cues to Eventhouse (queued ingestion)")
    s.add_argument("--kusto-query-uri", default=None)
    s.add_argument("--kusto-ingest-uri", default=None)
    s.add_argument("--kusto-database", default=None)
    s.add_argument("--consumer-group", default="$Default")
    s.add_argument("--from-start", action="store_true", help="eventhub: read from the beginning of retention instead of new events only")
    s.add_argument("--follow", action="store_true", help="eventhouse: keep polling for new rows")
    s.add_argument("--agents", choices=["off", "fake", "foundry", "framework"], default="off",
                   help="write the ghost text with the agent team: foundry (OpenAI client on the Foundry endpoint), framework (Microsoft Agent Framework), fake (offline)")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_forkd)

    s = sub.add_parser("explain", help="run one fork through Explainer, Localiser and Verifier and print the result")
    s.add_argument("match", help="path prefix, e.g. data/matches/RAV-IRO-1000")
    s.add_argument("--league", default="data/league.json")
    s.add_argument("--data", default="data")
    s.add_argument("--grid", default="data/threat_grid.json")
    s.add_argument("--event-id", type=int, default=None)
    s.add_argument("--rollouts", type=int, default=120)
    s.add_argument("--horizon", type=int, default=20)
    s.add_argument("--agents", choices=["fake", "foundry", "framework"], default="foundry")
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_explain)

    s = sub.add_parser("recap", help="full-time recap from a forkd record file")
    s.add_argument("records", help="the JSONL written by forkd --out")
    s.add_argument("--data", default="data")
    s.add_argument("--agents", choices=["fake", "foundry", "framework"], default="foundry")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--score", default=None, help="override the final score, e.g. 2-1")
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_recap)

    s = sub.add_parser("play", help="serve the renderer and replay a match with baseline and fork cues")
    s.add_argument("match", help="path prefix, e.g. data/matches/NBU-HAR-1000")
    s.add_argument("--league", default="data/league.json")
    s.add_argument("--speed", type=float, default=20.0, help="match seconds per wall second")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--forks", type=int, default=3, help="fork the N highest-xG open-play shots before kick-off")
    s.add_argument("--rollouts", type=int, default=200)
    s.add_argument("--horizon", type=int, default=20)
    s.add_argument("--loop", action="store_true", help="replay the match again when it ends")
    s.set_defaults(fn=cmd_play)

    s = sub.add_parser("serve", help="serve the renderer only; feed it over HTTP")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=cmd_serve)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
