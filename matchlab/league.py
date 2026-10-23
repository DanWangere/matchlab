"""A fictional league: 20 clubs, 18 players each, formations with nominal positions.

Nothing here is a real club or a real person. Calibrate team rates against a
real match-level dataset by editing the ranges in `make_league`, not by
importing that data.
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from typing import Optional

CLUB_NAMES = [
    ("Northbridge United", "NBU"), ("Harrowdale Town", "HAR"), ("Saltmarsh Athletic", "SAL"),
    ("Kingsmere City", "KMC"), ("Ravenscar Rovers", "RAV"), ("Westhollow FC", "WHO"),
    ("Ashcombe Wanderers", "ASH"), ("Dunmere Albion", "DUN"), ("Brightwater FC", "BRW"),
    ("Oldcastle Park", "OLD"), ("Fenwick Rangers", "FEN"), ("Thornbury United", "THU"),
    ("Eastgate Town", "EAS"), ("Millbrook Athletic", "MIL"), ("Cobbleton City", "COB"),
    ("Larkhill FC", "LAR"), ("Greywater United", "GRE"), ("Stonecross Rovers", "STO"),
    ("Ironhaven Town", "IRO"), ("Wrenfield Albion", "WRE"),
]

FIRST_NAMES = [
    "Amani", "Tomas", "Kwame", "Luca", "Mateo", "Idris", "Jonas", "Rafael", "Emeka", "Noah",
    "Yusuf", "Arlo", "Baraka", "Elias", "Felix", "Hamza", "Ivan", "Jabari", "Kai", "Leon",
    "Moussa", "Nikolai", "Omar", "Pietro", "Quentin", "Ruben", "Sefu", "Teo", "Umar", "Viktor",
    "Wanjiru", "Xavi", "Yann", "Zane", "Adebayo", "Bruno", "Cyrus", "Dani", "Ezra", "Farouk",
]
SYLLABLES = ["ka", "mo", "ren", "tu", "va", "lo", "sha", "ni", "bel", "dor", "fa", "gri", "hol", "jen", "kir", "lum", "mar", "nor", "pel", "ros", "tan", "vin", "wes", "yor", "zan"]

# Nominal positions in the attack frame (x towards 105), by formation. Order matters: index 0 is the goalkeeper.
FORMATIONS: dict[str, list[tuple[str, float, float]]] = {
    "4-3-3": [("GK", 5, 34), ("LB", 25, 10), ("LCB", 20, 24), ("RCB", 20, 44), ("RB", 25, 58),
              ("LCM", 45, 20), ("CM", 42, 34), ("RCM", 45, 48), ("LW", 75, 10), ("ST", 80, 34), ("RW", 75, 58)],
    "4-2-3-1": [("GK", 5, 34), ("LB", 25, 10), ("LCB", 20, 24), ("RCB", 20, 44), ("RB", 25, 58),
                ("LDM", 40, 24), ("RDM", 40, 44), ("LAM", 65, 14), ("CAM", 65, 34), ("RAM", 65, 54), ("ST", 82, 34)],
    "4-4-2": [("GK", 5, 34), ("LB", 25, 10), ("LCB", 20, 24), ("RCB", 20, 44), ("RB", 25, 58),
              ("LM", 50, 10), ("LCM", 45, 26), ("RCM", 45, 42), ("RM", 50, 58), ("LS", 78, 26), ("RS", 78, 42)],
    "3-5-2": [("GK", 5, 34), ("LCB", 20, 18), ("CB", 18, 34), ("RCB", 20, 50), ("LWB", 45, 6),
              ("LCM", 45, 24), ("CM", 42, 34), ("RCM", 45, 44), ("RWB", 45, 62), ("LS", 78, 28), ("RS", 78, 40)],
}


def role_group(role: str) -> str:
    if role == "GK":
        return "GK"
    if role in ("LB", "RB", "LCB", "RCB", "CB", "LWB", "RWB"):
        return "DEF"
    if role in ("LW", "RW", "ST", "LS", "RS", "LAM", "RAM", "CAM"):
        return "FWD"
    return "MID"


@dataclass
class Player:
    player_id: str
    name: str
    shirt: int
    role: str
    x: float
    y: float
    passing: float
    shooting: float
    pressing: float
    pace: float
    stamina: float
    starter: bool


@dataclass
class Team:
    team_id: str
    name: str
    short_name: str
    rating: float
    possession: float
    press_height: float
    direct: float
    formation: str
    stadium: str
    players: list[Player] = field(default_factory=list)

    def starters(self) -> list[Player]:
        return [p for p in self.players if p.starter]

    def bench(self) -> list[Player]:
        return [p for p in self.players if not p.starter]

    def player(self, player_id: str) -> Player:
        for p in self.players:
            if p.player_id == player_id:
                return p
        raise KeyError(player_id)


@dataclass
class League:
    seed: int
    teams: list[Team]

    def team(self, team_id: str) -> Team:
        for t in self.teams:
            if t.team_id == team_id:
                return t
        raise KeyError(team_id)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)

    @staticmethod
    def from_json(text: str) -> "League":
        d = json.loads(text)
        teams = []
        for t in d["teams"]:
            players = [Player(**p) for p in t.pop("players")]
            teams.append(Team(**t, players=players))
        return League(seed=d["seed"], teams=teams)


def _surname(rng: random.Random) -> str:
    n = rng.choice([2, 2, 3])
    s = "".join(rng.choice(SYLLABLES) for _ in range(n))
    return s.capitalize()


def _attr(rng: random.Random, centre: float, spread: float = 0.08) -> float:
    return round(min(0.98, max(0.2, rng.gauss(centre, spread))), 3)


def make_league(seed: int = 7) -> League:
    rng = random.Random(seed)
    teams: list[Team] = []
    for i, (name, short) in enumerate(CLUB_NAMES):
        # A few strong clubs, a broad middle, a few weak ones.
        rating = round(min(0.95, max(0.3, rng.betavariate(2.2, 2.0) * 0.75 + 0.2)), 3)
        possession = round(min(0.9, max(0.25, 0.25 + 0.55 * rating + rng.gauss(0, 0.07))), 3)
        press_height = round(min(0.9, max(0.25, rng.uniform(0.3, 0.85))), 3)
        direct = round(min(0.9, max(0.1, 1.0 - possession + rng.gauss(0, 0.08))), 3)
        formation = rng.choice(list(FORMATIONS))
        team_id = f"t{i + 1:02d}"
        team = Team(team_id, name, short, rating, possession, press_height, direct, formation,
                    stadium=f"{name.split()[0]} Park")
        slots = FORMATIONS[formation]
        shirt = 1
        centre = 0.38 + 0.5 * rating
        for role, x, y in slots:
            team.players.append(Player(
                player_id=f"{team_id}p{shirt:02d}", name=f"{rng.choice(FIRST_NAMES)} {_surname(rng)}",
                shirt=shirt, role=role, x=x, y=y,
                passing=_attr(rng, centre + (0.05 if role_group(role) == "MID" else 0)),
                shooting=_attr(rng, centre + (0.08 if role_group(role) == "FWD" else -0.1)),
                pressing=_attr(rng, centre + 0.15 * (press_height - 0.5)),
                pace=_attr(rng, centre + (0.06 if role_group(role) == "FWD" else 0)),
                stamina=_attr(rng, centre, 0.1),
                starter=True,
            ))
            shirt += 1
        # Seven on the bench: a goalkeeper, two defenders, two midfielders, two forwards.
        bench_roles = ["GK", "LCB", "RB", "CM", "LCM", "ST", "LW"]
        for role in bench_roles:
            slot = next((s for s in slots if s[0] == role), None) or next(s for s in slots if role_group(s[0]) == role_group(role))
            team.players.append(Player(
                player_id=f"{team_id}p{shirt:02d}", name=f"{rng.choice(FIRST_NAMES)} {_surname(rng)}",
                shirt=shirt, role=slot[0], x=slot[1], y=slot[2],
                passing=_attr(rng, centre - 0.04), shooting=_attr(rng, centre - 0.06 + (0.08 if role_group(role) == "FWD" else 0)),
                pressing=_attr(rng, centre), pace=_attr(rng, centre), stamina=_attr(rng, centre + 0.05, 0.1),
                starter=False,
            ))
            shirt += 1
        teams.append(team)
    return League(seed=seed, teams=teams)
