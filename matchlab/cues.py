"""The cue contract: timed, machine-readable overlay instructions.

A cue is what every producer (the baseline rules here, the fork service, the Undertow ledger,
the agents) emits and what every renderer (the web overlay, a broadcast graphics partner, a
voice track) consumes. Nothing downstream reads raw events to decide what to show.

Fields:
  t_start_s, period, duration_s   when, on the match clock
  layer                           tag | card | lower_third | ghost | dial | ledger
  text[lang]                      the casual line, per language
  text_analyst[lang]              the line with numbers, per language (falls back to text)
  geometry                        pitch coordinates in the acting team's attack frame, plus side
  modes                           which viewer modes may show it: analyst, casual, club, player
  team_id, player_id              for club and player filtering
  priority                        1 (never drop) to 9 (filler)
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .schema import Event

LAYERS = ("tag", "card", "lower_third", "ghost", "dial", "ledger")
MODES = ("analyst", "casual", "club", "player")
LANGS = ("en", "fr", "es")


@dataclass
class Cue:
    cue_id: str
    match_id: str
    period: int
    t_start_s: float
    duration_s: float
    layer: str
    text: dict[str, str]
    text_analyst: dict[str, str] = field(default_factory=dict)
    geometry: dict[str, Any] = field(default_factory=dict)
    modes: list[str] = field(default_factory=lambda: list(MODES))
    priority: int = 5
    team_id: Optional[str] = None
    player_id: Optional[str] = None
    event_id: Optional[int] = None
    source: str = "baseline"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["t_start_s"] = round(self.t_start_s, 2)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------------------------ templates

T: dict[str, dict[str, str]] = {
    "goal": {"en": "GOAL! {name} for {team}. {home} {h}-{a} {away}",
             "fr": "BUT ! {name} pour {team}. {home} {h}-{a} {away}",
             "es": "¡GOL! {name} para {team}. {home} {h}-{a} {away}"},
    "goal_analyst": {"en": "GOAL! {name} ({team}), xG {xg:.2f} from {dist:.0f} m. {home} {h}-{a} {away}",
                     "fr": "BUT ! {name} ({team}), xG {xg:.2f} à {dist:.0f} m. {home} {h}-{a} {away}",
                     "es": "¡GOL! {name} ({team}), xG {xg:.2f} desde {dist:.0f} m. {home} {h}-{a} {away}"},
    "big_chance": {"en": "Big chance for {name}: {outcome}",
                   "fr": "Grosse occasion pour {name} : {outcome}",
                   "es": "Gran ocasión para {name}: {outcome}"},
    "big_chance_analyst": {"en": "{name}: xG {xg:.2f} from {dist:.0f} m, {outcome}",
                           "fr": "{name} : xG {xg:.2f} à {dist:.0f} m, {outcome}",
                           "es": "{name}: xG {xg:.2f} desde {dist:.0f} m, {outcome}"},
    "hard_pass": {"en": "{name} finds {receiver} with a pass most would not try",
                  "fr": "{name} trouve {receiver} d'une passe que peu tenteraient",
                  "es": "{name} encuentra a {receiver} con un pase que pocos intentarían"},
    "hard_pass_analyst": {"en": "{name} to {receiver}: {length:.0f} m, difficulty {diff:.2f}{pressure}",
                          "fr": "{name} vers {receiver} : {length:.0f} m, difficulté {diff:.2f}{pressure}",
                          "es": "{name} a {receiver}: {length:.0f} m, dificultad {diff:.2f}{pressure}"},
    "under_pressure": {"en": ", under pressure", "fr": ", sous pression", "es": ", bajo presión"},
    "fast_shot": {"en": "{name}: {kmh:.0f} km/h, fastest shot of the match",
                  "fr": "{name} : {kmh:.0f} km/h, tir le plus rapide du match",
                  "es": "{name}: {kmh:.0f} km/h, el disparo más rápido del partido"},
    "sub": {"en": "{team}: {on} on, {off} off",
            "fr": "{team} : {on} entre, {off} sort",
            "es": "{team}: entra {on}, sale {off}"},
    "card": {"en": "Yellow card: {name} ({team})",
             "fr": "Carton jaune : {name} ({team})",
             "es": "Tarjeta amarilla: {name} ({team})"},
    "penalty": {"en": "Penalty to {team}", "fr": "Penalty pour {team}", "es": "Penalti para {team}"},
    "half_time": {"en": "Half-time: {home} {h}-{a} {away}", "fr": "Mi-temps : {home} {h}-{a} {away}", "es": "Descanso: {home} {h}-{a} {away}"},
    "full_time": {"en": "Full-time: {home} {h}-{a} {away}", "fr": "Fin du match : {home} {h}-{a} {away}", "es": "Final: {home} {h}-{a} {away}"},
    "momentum_home": {"en": "{team} on top", "fr": "{team} domine", "es": "{team} domina"},
    "momentum_even": {"en": "Evenly matched", "fr": "Équilibré", "es": "Igualado"},
    "momentum_analyst": {"en": "Momentum {value:+.2f} ({team} threat last 5 min: {h:.2f} vs {a:.2f})",
                         "fr": "Momentum {value:+.2f} (menace {team} sur 5 min : {h:.2f} vs {a:.2f})",
                         "es": "Momentum {value:+.2f} (amenaza {team} últimos 5 min: {h:.2f} vs {a:.2f})"},
    "milestone_passes": {"en": "{name}: {n} passes completed", "fr": "{name} : {n} passes réussies", "es": "{name}: {n} pases completados"},
    "milestone_carry": {"en": "{name} has carried the ball {m:.0f} m", "fr": "{name} a parcouru {m:.0f} m balle au pied", "es": "{name} ha conducido {m:.0f} m"},
    "milestone_hard": {"en": "{name}: {n} hard passes completed", "fr": "{name} : {n} passes difficiles réussies", "es": "{name}: {n} pases difíciles completados"},
    "fork": {"en": "{name} {took}. {alt} was the better option.",
             "fr": "{name} {took}. {alt} était la meilleure option.",
             "es": "{name} {took}. {alt} era la mejor opción."},
    "fork_fine": {"en": "{name} {took}, and it was the right call.",
                  "fr": "{name} {took}, et c'était le bon choix.",
                  "es": "{name} {took}, y fue la decisión correcta."},
    "fork_analyst": {"en": "{name} {took}: {p_taken:.0%} chance of a goal within {h} events. {alt}: {p_alt:.0%}. Regret {regret:+.2f}.",
                     "fr": "{name} {took} : {p_taken:.0%} de chance de but sur {h} actions. {alt} : {p_alt:.0%}. Regret {regret:+.2f}.",
                     "es": "{name} {took}: {p_taken:.0%} de probabilidad de gol en {h} acciones. {alt}: {p_alt:.0%}. Arrepentimiento {regret:+.2f}."},
    "took_shot": {"en": "shot", "fr": "a tiré", "es": "disparó"},
    "took_pass": {"en": "passed to {receiver}", "fr": "a passé à {receiver}", "es": "pasó a {receiver}"},
    "took_carry": {"en": "carried on", "fr": "a continué balle au pied", "es": "siguió conduciendo"},
    "took_clearance": {"en": "cleared", "fr": "a dégagé", "es": "despejó"},
    "alt_shot": {"en": "A shot", "fr": "Un tir", "es": "Un disparo"},
    "alt_pass": {"en": "A pass to {receiver}", "fr": "Une passe vers {receiver}", "es": "Un pase a {receiver}"},
    "alt_carry": {"en": "Carrying on", "fr": "Continuer balle au pied", "es": "Seguir conduciendo"},
    "alt_clearance": {"en": "A clearance", "fr": "Un dégagement", "es": "Un despeje"},
    "outcome_goal": {"en": "goal", "fr": "but", "es": "gol"},
    "outcome_saved": {"en": "saved", "fr": "arrêté", "es": "parado"},
    "outcome_blocked": {"en": "blocked", "fr": "contré", "es": "bloqueado"},
    "outcome_off_target": {"en": "off target", "fr": "non cadré", "es": "fuera"},
}


def _t(key: str, **kw: Any) -> dict[str, str]:
    return {lang: T[key][lang].format(**kw) for lang in LANGS}


def _tv(key: str, lang: str, **kw: Any) -> str:
    return T[key][lang].format(**kw)


# ------------------------------------------------------------------ names

class Names:
    """Player and team names from match metadata."""

    def __init__(self, meta: dict[str, Any]):
        self.meta = meta
        self.team_name = {meta["home"]["team_id"]: meta["home"]["name"], meta["away"]["team_id"]: meta["away"]["name"]}
        self.short = {meta["home"]["team_id"]: meta["home"]["short_name"], meta["away"]["team_id"]: meta["away"]["short_name"]}
        self.player_name: dict[str, str] = {}
        self.player_team: dict[str, str] = {}
        for side in ("home", "away"):
            for p in meta[side]["lineup"]:
                self.player_name[p["player_id"]] = p["name"]
                self.player_team[p["player_id"]] = meta[side]["team_id"]
        self.home_id, self.away_id = meta["home"]["team_id"], meta["away"]["team_id"]

    def player(self, pid: Optional[str]) -> str:
        return self.player_name.get(pid or "", pid or "?")

    def team(self, tid: Optional[str]) -> str:
        return self.team_name.get(tid or "", tid or "?")

    def side(self, tid: str) -> str:
        return "home" if tid == self.home_id else "away"


# ------------------------------------------------------------------ baseline cues from the raw stream

def cues_from_events(events: list[Event], meta: dict[str, Any]) -> list[Cue]:
    """Deterministic cues from the stream alone: the floor every renderer gets before any agent runs."""
    n = Names(meta)
    cues: list[Cue] = []
    home, away = n.team(n.home_id), n.team(n.away_id)
    fastest_shot = 0.0
    passes_done: dict[str, int] = defaultdict(int)
    hard_done: dict[str, int] = defaultdict(int)
    carried: dict[str, float] = defaultdict(float)
    carry_mark: dict[str, int] = defaultdict(int)
    k = 0

    def cue(ev: Event, layer: str, duration: float, text: dict[str, str], text_analyst: Optional[dict[str, str]] = None,
            modes: Optional[list[str]] = None, priority: int = 5, geometry: Optional[dict[str, Any]] = None,
            team_id: Optional[str] = None, player_id: Optional[str] = None) -> Cue:
        nonlocal k
        k += 1
        c = Cue(cue_id=f"{ev.match_id}-b{k:05d}", match_id=ev.match_id, period=ev.period, t_start_s=ev.clock_s,
                duration_s=duration, layer=layer, text=text, text_analyst=text_analyst or {},
                geometry=geometry or {}, modes=modes or list(MODES), priority=priority,
                team_id=team_id if team_id is not None else ev.team_id, player_id=player_id if player_id is not None else ev.player_id,
                event_id=ev.event_id)
        cues.append(c)
        return c

    for idx, ev in enumerate(events):
        name = n.player(ev.player_id)
        team = n.team(ev.team_id)
        geo = {"side": n.side(ev.team_id), "x": ev.x, "y": ev.y, "end_x": ev.end_x, "end_y": ev.end_y}
        if ev.type == "shot":
            dist = ((105 - (ev.x or 0)) ** 2 + (34 - (ev.y or 0)) ** 2) ** 0.5
            outcome = {lang: _tv(f"outcome_{ev.outcome}", lang) for lang in LANGS} if ev.outcome in ("goal", "saved", "blocked", "off_target") else {lang: ev.outcome or "" for lang in LANGS}
            if ev.outcome == "goal":
                cue(ev, "lower_third", 10.0,
                    _t("goal", name=name, team=team, home=home, away=away, h=ev.score_home, a=ev.score_away),
                    _t("goal_analyst", name=name, team=team, home=home, away=away, h=ev.score_home, a=ev.score_away, xg=ev.xg or 0, dist=dist),
                    priority=1, geometry=geo)
            elif (ev.xg or 0) >= 0.2:
                cue(ev, "card", 5.0,
                    {lang: _tv("big_chance", lang, name=name, outcome=outcome[lang]) for lang in LANGS},
                    {lang: _tv("big_chance_analyst", lang, name=name, xg=ev.xg or 0, dist=dist, outcome=outcome[lang]) for lang in LANGS},
                    priority=3, geometry=geo)
            if ev.subtype == "penalty":
                cue(ev, "card", 4.0, _t("penalty", team=team), priority=2, geometry=geo)
            if (ev.ball_speed_mps or 0) > fastest_shot and ev.subtype != "header":
                fastest_shot = ev.ball_speed_mps or 0
                if fastest_shot * 3.6 >= 95:
                    cue(ev, "tag", 4.0, _t("fast_shot", name=name, kmh=fastest_shot * 3.6), modes=["analyst", "casual", "club", "player"], priority=6, geometry=geo)
        elif ev.type == "pass":
            if ev.outcome == "complete" and ev.player_id:
                passes_done[ev.player_id] += 1
                if passes_done[ev.player_id] in (25, 50, 75, 100):
                    cue(ev, "tag", 4.0, _t("milestone_passes", name=name, n=passes_done[ev.player_id]), modes=["player"], priority=7)
                if (ev.pass_difficulty or 0) >= 0.5:
                    hard_done[ev.player_id] += 1
                    receiver_id = None
                    # The receiver is the next on-ball actor of the same team; look ahead cheaply.
                    for nxt in events[idx + 1: idx + 4]:
                        if nxt.team_id == ev.team_id and nxt.type in ("pass", "carry", "shot", "clearance"):
                            receiver_id = nxt.player_id
                            break
                    receiver = n.player(receiver_id)
                    pressure = {lang: (_tv("under_pressure", lang) if ev.under_pressure else "") for lang in LANGS}
                    cue(ev, "tag", 4.0,
                        _t("hard_pass", name=name, receiver=receiver),
                        {lang: _tv("hard_pass_analyst", lang, name=name, receiver=receiver, length=ev.pass_length_m or 0, diff=ev.pass_difficulty or 0, pressure=pressure[lang]) for lang in LANGS},
                        modes=["analyst", "club", "player"], priority=6, geometry=geo)
                    if hard_done[ev.player_id] in (3, 6, 10):
                        cue(ev, "tag", 4.0, _t("milestone_hard", name=name, n=hard_done[ev.player_id]), modes=["player"], priority=7)
        elif ev.type == "carry" and ev.player_id and ev.end_x is not None:
            carried[ev.player_id] += ((ev.end_x - (ev.x or 0)) ** 2 + ((ev.end_y or 0) - (ev.y or 0)) ** 2) ** 0.5
            mark = int(carried[ev.player_id] // 100)
            if mark > carry_mark[ev.player_id]:
                carry_mark[ev.player_id] = mark
                cue(ev, "tag", 4.0, _t("milestone_carry", name=name, m=mark * 100), modes=["player"], priority=7)
        elif ev.type == "substitution":
            cue(ev, "card", 5.0, _t("sub", team=team, on=n.player(ev.detail.get("on")), off=n.player(ev.detail.get("off"))), priority=4)
        elif ev.type == "card":
            cue(ev, "card", 5.0, _t("card", name=name, team=team), priority=3, geometry=geo)
        elif ev.type == "period_end":
            key = "half_time" if ev.detail.get("period") == 1 else "full_time"
            cue(ev, "lower_third", 12.0, _t(key, home=home, away=away, h=ev.score_home, a=ev.score_away), priority=1, team_id="", player_id="")

    cues += momentum_cues(events, meta)
    cues.sort(key=lambda c: (c.period, c.t_start_s, c.priority))
    return cues


def momentum_cues(events: list[Event], meta: dict[str, Any], window_s: float = 60.0) -> list[Cue]:
    """A dial per minute: the same five-minute rolling threat swing as the Eventhouse Momentum function."""
    n = Names(meta)
    threat: dict[tuple[int, str], float] = defaultdict(float)
    period_of: dict[int, int] = {}
    for ev in events:
        w = int(ev.clock_s // window_s)
        period_of[w] = ev.period
        if ev.type == "shot":
            threat[(w, ev.team_id)] += 2.0 * (ev.xg or 0) + 0.25
        elif ev.type in ("pass", "carry") and ev.outcome == "complete" and (ev.x or 0) < 70 and (ev.end_x or 0) >= 70:
            threat[(w, ev.team_id)] += 0.08
    if not period_of:
        return []
    out: list[Cue] = []
    swings: list[float] = []
    home, away = n.home_id, n.away_id
    for w in range(0, max(period_of) + 1):
        h, a = threat[(w, home)], threat[(w, away)]
        swings.append(h - a)
        recent = swings[-5:]
        m = sum(recent) / len(recent)
        value = max(-1.0, min(1.0, m / 0.5))
        leader = home if m > 0.08 else (away if m < -0.08 else None)
        text = _t("momentum_home", team=n.team(leader)) if leader else _t("momentum_even")
        text_a = _t("momentum_analyst", value=m, team=n.short[home], h=sum(threat[(x, home)] for x in range(max(0, w - 4), w + 1)),
                    a=sum(threat[(x, away)] for x in range(max(0, w - 4), w + 1)))
        out.append(Cue(cue_id=f"{meta['match_id']}-m{w:03d}", match_id=meta["match_id"], period=period_of.get(w, 1),
                       t_start_s=w * window_s + window_s - 0.01, duration_s=window_s, layer="dial", text=text,
                       text_analyst=text_a, geometry={"value": round(value, 3), "home_threat": round(h, 3), "away_threat": round(a, 3)},
                       modes=list(MODES), priority=5, team_id=leader or "", player_id="", source="baseline"))
    return out


# ------------------------------------------------------------------ the ghost: Otherwise's cue

def _describe_action(r: dict[str, Any], names: Names, lang: str, as_alt: bool) -> str:
    kind = r["type"]
    prefix = "alt" if as_alt else "took"
    if kind == "pass":
        return _tv(f"{prefix}_pass", lang, receiver=names.player(r.get("receiver")))
    return _tv(f"{prefix}_{kind}", lang)


def cue_from_fork(record: dict[str, Any], meta: dict[str, Any]) -> Cue:
    """Turn a fork record into a ghost cue: the path not taken, drawn on the pitch, with the gap in words."""
    n = Names(meta)
    taken, alt = record["taken"], record["best_untaken"]
    name = n.player(record["player_id"])
    regret = record["regret"]
    worth_it = regret >= 0.03
    text, text_a = {}, {}
    for lang in LANGS:
        took = _describe_action(taken, n, lang, as_alt=False)
        if alt and worth_it:
            alt_s = _describe_action(alt, n, lang, as_alt=True)
            text[lang] = _tv("fork", lang, name=name, took=took, alt=alt_s)
            text_a[lang] = _tv("fork_analyst", lang, name=name, took=took, alt=alt_s, p_taken=taken["p_goal_for"],
                               p_alt=alt["p_goal_for"], h=record["horizon_events"], regret=regret)
        else:
            text[lang] = _tv("fork_fine", lang, name=name, took=took)
            text_a[lang] = _tv("fork_analyst", lang, name=name, took=took, alt=_describe_action(alt, n, lang, True) if alt else "-",
                               p_taken=taken["p_goal_for"], p_alt=alt["p_goal_for"] if alt else 0.0, h=record["horizon_events"], regret=regret)
    geometry: dict[str, Any] = {"side": record["side"], "x": record["x"], "y": record["y"], "kind": alt["type"] if alt else None,
                                "worth_it": worth_it}
    if alt:
        if alt["type"] == "shot":
            geometry["end_x"], geometry["end_y"] = 105.0, 34.0
        else:
            geometry["end_x"], geometry["end_y"] = alt.get("end_x"), alt.get("end_y")
        geometry["candidates"] = [{"type": c["type"], "subtype": c.get("subtype"), "receiver": c.get("receiver"),
                                   "end_x": c.get("end_x"), "end_y": c.get("end_y"), "p_goal_for": c["p_goal_for"],
                                   "value": c["value"], "taken": c["taken"]} for c in record["candidates"]]
    return Cue(cue_id=f"{record['match_id']}-f{record['event_id']:05d}", match_id=record["match_id"], period=record["period"],
               t_start_s=record["clock_s"] + 4.0, duration_s=9.0 if worth_it else 6.0, layer="ghost", text=text, text_analyst=text_a,
               geometry=geometry, modes=list(MODES), priority=2 if worth_it else 6, team_id=record["team_id"],
               player_id=record["player_id"], event_id=record["event_id"], source="otherwise")
