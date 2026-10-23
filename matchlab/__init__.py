"""matchlab: a synthetic football league with hidden ground truth and a rewindable simulator."""
from .league import League, make_league
from .schema import Event, JSON_SCHEMA
from .sim import Simulator

__all__ = ["League", "make_league", "Event", "JSON_SCHEMA", "Simulator"]
__version__ = "0.1.0"
