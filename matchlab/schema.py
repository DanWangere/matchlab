"""The contract every other component consumes: event, metadata and truth records.

Coordinates are metres on a 105 x 68 pitch, in the acting team's attack frame
(the team attacks towards x = 105). A renderer flips the away team.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

PITCH_LENGTH = 105.0
PITCH_WIDTH = 68.0
GOAL_Y = PITCH_WIDTH / 2
POST_HALF_WIDTH = 3.66

EVENT_TYPES = (
    "pass", "carry", "shot", "tackle", "interception", "clearance", "pressure",
    "foul", "possession_change", "substitution", "card", "kick_off", "period_end",
)
PASS_SUBTYPES = ("short", "long", "through", "cross", "switch", "corner", "free_kick", "goal_kick", "throw_in")
SHOT_SUBTYPES = ("open_play", "set_piece", "penalty", "header")
PRESSURE_SUBTYPES = ("counter_press", "high_press", "mid_block")
OUTCOMES = ("complete", "incomplete", "goal", "saved", "blocked", "off_target", "won", "lost")
PHASES = ("build_up", "progression", "final_third", "transition", "set_piece")


@dataclass
class Event:
    match_id: str
    event_id: int
    period: int
    clock_s: float
    ts: str
    team_id: str
    player_id: Optional[str]
    type: str
    subtype: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    end_x: Optional[float] = None
    end_y: Optional[float] = None
    outcome: Optional[str] = None
    under_pressure: bool = False
    pass_length_m: Optional[float] = None
    pass_difficulty: Optional[float] = None
    ball_speed_mps: Optional[float] = None
    xg: Optional[float] = None
    possession_id: Optional[str] = None
    phase: Optional[str] = None
    related_event_id: Optional[int] = None
    score_home: int = 0
    score_away: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("x", "y", "end_x", "end_y", "pass_length_m", "pass_difficulty", "ball_speed_mps", "xg", "clock_s"):
            if d[k] is not None:
                d[k] = round(d[k], 2 if k != "xg" and k != "pass_difficulty" else 3)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Event":
        return Event(**d)


JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "matchlab event",
    "type": "object",
    "required": ["match_id", "event_id", "period", "clock_s", "ts", "team_id", "type", "score_home", "score_away"],
    "properties": {
        "match_id": {"type": "string"},
        "event_id": {"type": "integer", "description": "monotonic within a match"},
        "period": {"type": "integer", "enum": [1, 2]},
        "clock_s": {"type": "number", "description": "match clock in seconds; period 2 starts at 2700"},
        "ts": {"type": "string", "format": "date-time", "description": "wall clock for live replay"},
        "team_id": {"type": "string"},
        "player_id": {"type": ["string", "null"]},
        "type": {"type": "string", "enum": list(EVENT_TYPES)},
        "subtype": {"type": ["string", "null"], "enum": list(PASS_SUBTYPES + SHOT_SUBTYPES + PRESSURE_SUBTYPES) + [None]},
        "x": {"type": ["number", "null"], "minimum": 0, "maximum": PITCH_LENGTH},
        "y": {"type": ["number", "null"], "minimum": 0, "maximum": PITCH_WIDTH},
        "end_x": {"type": ["number", "null"]},
        "end_y": {"type": ["number", "null"]},
        "outcome": {"type": ["string", "null"], "enum": list(OUTCOMES) + [None]},
        "under_pressure": {"type": "boolean"},
        "pass_length_m": {"type": ["number", "null"]},
        "pass_difficulty": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "ball_speed_mps": {"type": ["number", "null"]},
        "xg": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "possession_id": {"type": ["string", "null"]},
        "phase": {"type": ["string", "null"], "enum": list(PHASES) + [None]},
        "related_event_id": {"type": ["integer", "null"]},
        "score_home": {"type": "integer"},
        "score_away": {"type": "integer"},
        "detail": {"type": "object"},
    },
}
