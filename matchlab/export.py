"""Flatten the nested files (league, match metadata, truth) into one-row-per-record JSONL,
which is what Eventhouse and Lakehouse ingestion want."""
from __future__ import annotations

import glob
import json
import os
from typing import Any, Iterable


def _write_jsonl(path: str, rows: Iterable[dict[str, Any]]) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
            n += 1
    return n


def export_all(data_dir: str, out_dir: str) -> dict[str, int]:
    """Writes teams, players, matches, lineups, truth_minutes, truth_changes and truth_schedules.
    Returns row counts per file."""
    os.makedirs(out_dir, exist_ok=True)
    counts: dict[str, int] = {}

    with open(os.path.join(data_dir, "league.json"), encoding="utf-8") as f:
        league = json.load(f)
    teams, players = [], []
    for t in league["teams"]:
        teams.append({k: v for k, v in t.items() if k != "players"} | {"league_seed": league["seed"]})
        for p in t["players"]:
            players.append({"team_id": t["team_id"], **p})
    counts["teams"] = _write_jsonl(os.path.join(out_dir, "teams.jsonl"), teams)
    counts["players"] = _write_jsonl(os.path.join(out_dir, "players.jsonl"), players)

    matches, lineups = [], []
    for path in sorted(glob.glob(os.path.join(data_dir, "matches", "*.meta.json"))):
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        matches.append({
            "match_id": m["match_id"], "seed": m["seed"], "league_seed": m["league_seed"], "kickoff": m["kickoff"],
            "venue": m["venue"],
            "home_team_id": m["home"]["team_id"], "home_name": m["home"]["name"], "home_short": m["home"]["short_name"],
            "home_formation": m["home"]["formation"],
            "away_team_id": m["away"]["team_id"], "away_name": m["away"]["name"], "away_short": m["away"]["short_name"],
            "away_formation": m["away"]["formation"],
        })
        for side in ("home", "away"):
            for p in m[side]["lineup"]:
                lineups.append({"match_id": m["match_id"], "side": side, "team_id": m[side]["team_id"], **p})
    counts["matches"] = _write_jsonl(os.path.join(out_dir, "matches.jsonl"), matches)
    counts["lineups"] = _write_jsonl(os.path.join(out_dir, "lineups.jsonl"), lineups)

    minutes, changes, schedules = [], [], []
    for path in sorted(glob.glob(os.path.join(data_dir, "truth", "*.truth.json"))):
        with open(path, encoding="utf-8") as f:
            t = json.load(f)
        mid = t["match_id"]
        for row in t["minutes"]:
            minutes.append({"match_id": mid, **row})
        for row in t["instruction_changes"]:
            changes.append({"match_id": mid, **row})
        for team_id, sch in t["schedules"].items():
            for minute, (press, risk) in enumerate(zip(sch["press_intent"], sch["risk_appetite"])):
                schedules.append({"match_id": mid, "team_id": team_id, "minute": minute,
                                  "scheduled_press_intent": press, "scheduled_risk_appetite": risk})
    counts["truth_minutes"] = _write_jsonl(os.path.join(out_dir, "truth_minutes.jsonl"), minutes)
    counts["truth_changes"] = _write_jsonl(os.path.join(out_dir, "truth_changes.jsonl"), changes)
    counts["truth_schedules"] = _write_jsonl(os.path.join(out_dir, "truth_schedules.jsonl"), schedules)
    return counts
