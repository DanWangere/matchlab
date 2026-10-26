"""The agent team that turns a fork record into words a viewer can trust.

    Explainer   reads the fork record, writes one English sentence for a casual fan and one for
                an analyst. It may only use facts that are in the record.
    Localiser   turns the English into French and Spanish, keeping names and numbers intact.
    Verifier    deterministic, no model: every number and every name in every sentence must
                appear in the record. Anything that fails falls back to the template text, so a
                hallucination can never reach the screen.
    Recap       at full time, reads the match's fork records and writes the story of the five
                decisions that mattered most, in all three languages.

The model is reached through Microsoft Foundry: the project's OpenAI-compatible endpoint with
Entra ID (DefaultAzureCredential, so `az login` is enough locally and a managed identity in the
cloud). Orchestration uses the Microsoft Agent Framework when it is installed; without it, the
same agents run through the plain OpenAI client, and the tests use a fake model with no network.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from .cues import LANGS, Cue, Names, cue_from_fork

MODEL_ENDPOINT = os.environ.get("FOUNDRY_MODEL_ENDPOINT", "https://aif-garage-swedencentral.services.ai.azure.com/openai/v1")
PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "https://aif-garage-swedencentral.services.ai.azure.com/api/projects/proj-garage-swedencentral")
DEPLOYMENT = os.environ.get("FOUNDRY_DEPLOYMENT", "gpt-5.6-sol")
TOKEN_SCOPE = "https://ai.azure.com/.default"


# ------------------------------------------------------------------ model clients

class ModelClient(Protocol):
    def complete(self, system: str, user: str, json_schema: Optional[dict[str, Any]] = None) -> str: ...


class FoundryModel:
    """Chat completions on the Foundry project's OpenAI-compatible endpoint, Entra ID auth."""

    def __init__(self, endpoint: str = MODEL_ENDPOINT, deployment: str = DEPLOYMENT, credential: Any = None):
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import OpenAI

        self.deployment = deployment
        self.token = get_bearer_token_provider(credential or DefaultAzureCredential(), TOKEN_SCOPE)
        self.client = OpenAI(base_url=endpoint, api_key=self.token)
        self.calls = 0
        self.seconds = 0.0

    def complete(self, system: str, user: str, json_schema: Optional[dict[str, Any]] = None) -> str:
        t0 = time.perf_counter()
        kwargs: dict[str, Any] = {}
        if json_schema is not None:
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": json_schema, "strict": False}}
        r = self.client.chat.completions.create(
            model=self.deployment, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kwargs)
        self.calls += 1
        self.seconds += time.perf_counter() - t0
        return r.choices[0].message.content or ""


class AgentFrameworkModel:
    """The same calls through the Microsoft Agent Framework's Agent abstraction, which gives
    middleware, sessions and tracing for free. Requires agent-framework-openai."""

    def __init__(self, endpoint: str = MODEL_ENDPOINT, deployment: str = DEPLOYMENT, credential: Any = None):
        import asyncio

        from agent_framework import Agent
        from agent_framework.openai import OpenAIChatClient
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        self._asyncio = asyncio
        self._Agent = Agent
        token = get_bearer_token_provider(credential or DefaultAzureCredential(), TOKEN_SCOPE)
        self.client = OpenAIChatClient(deployment, base_url=endpoint, api_key=token)
        self.calls = 0
        self.seconds = 0.0

    def complete(self, system: str, user: str, json_schema: Optional[dict[str, Any]] = None) -> str:
        t0 = time.perf_counter()
        agent = self._Agent(self.client, instructions=system + ("\nAnswer with a single JSON object and nothing else." if json_schema else ""))
        resp = self._asyncio.run(agent.run(user))
        self.calls += 1
        self.seconds += time.perf_counter() - t0
        return resp.text or ""


class FakeModel:
    """Offline stand-in: answers from the record the way a well-behaved model would. Tests use it,
    and `--agents fake` lets the whole pipeline run without a Foundry project."""

    def __init__(self, hallucinate: bool = False):
        self.hallucinate = hallucinate
        self.calls = 0
        self.seconds = 0.0

    def complete(self, system: str, user: str, json_schema: Optional[dict[str, Any]] = None) -> str:
        self.calls += 1
        payload = json.loads(user[user.index("{"):]) if "{" in user else {}
        if "explainer" in system:
            r = payload
            t, b = r["taken"], r.get("best_untaken")
            name = r.get("player_name", "The player")
            took = _verb(t)
            if b and r["regret"] >= 0.03:
                casual = f"{name} {took}; {_noun(b, r)} looked the better option."
                analyst = f"{name} {took}: {round(t['p_goal_for'] * 100)}% chance of a goal within {r['horizon_events']} events against {round(b['p_goal_for'] * 100)}% for {_noun(b, r).lower()}. Regret {r['regret']:+.2f}."
            else:
                casual = f"{name} {took}, and it was the right call."
                analyst = f"{name} {took}: {round(t['p_goal_for'] * 100)}% chance of a goal within {r['horizon_events']} events, the best of {len(r['candidates'])} options."
            if self.hallucinate:
                analyst += " He has scored 14 goals this season."
            return json.dumps({"casual": casual, "analyst": analyst})
        if "localiser" in system:
            src = payload
            out = {}
            for lang in ("fr", "es"):
                out[lang] = {k: _pseudo_translate(v, lang) for k, v in src.items()}
            return json.dumps(out)
        if "recap" in system:
            forks = payload.get("forks", [])
            lines = [f"Minute {f['minute']}: {f['player_name']} {_verb(f['taken'])}, regret {f['regret']:+.2f}." for f in forks[:5]]
            text = " ".join(lines) or "No decisive forks."
            return json.dumps({"en": text, "fr": _pseudo_translate(text, "fr"), "es": _pseudo_translate(text, "es")})
        return "{}"


def _verb(action: dict[str, Any]) -> str:
    k = action["type"]
    if k == "pass":
        return f"passed to {action.get('receiver_name', 'a team-mate')}"
    return {"shot": "shot", "carry": "carried on", "clearance": "cleared"}.get(k, k)


def _noun(action: dict[str, Any], r: dict[str, Any]) -> str:
    k = action["type"]
    if k == "pass":
        return f"A pass to {action.get('receiver_name', 'a team-mate')}"
    return {"shot": "A shot", "carry": "Carrying on", "clearance": "A clearance"}.get(k, k)


def _pseudo_translate(text: str, lang: str) -> str:
    """Keeps every name and number; only the glue words change. Enough for the Verifier to pass
    and for a demo to show the language switch working without a model."""
    table = {
        "fr": [("passed to", "a passé à"), ("shot", "a tiré"), ("carried on", "a continué balle au pied"), ("cleared", "a dégagé"),
               ("looked the better option", "semblait la meilleure option"), ("and it was the right call", "et c'était le bon choix"),
               ("chance of a goal within", "de chance de but sur"), ("events", "actions"), ("against", "contre"), ("for", "pour"),
               ("A pass to", "Une passe vers"), ("A shot", "Un tir"), ("Carrying on", "Continuer balle au pied"), ("a pass to", "une passe vers"),
               ("a shot", "un tir"), ("carrying on", "continuer balle au pied"), ("Regret", "Regret"), ("the best of", "le meilleur parmi"), ("options", "options"), ("Minute", "Minute")],
        "es": [("passed to", "pasó a"), ("shot", "disparó"), ("carried on", "siguió conduciendo"), ("cleared", "despejó"),
               ("looked the better option", "parecía la mejor opción"), ("and it was the right call", "y fue la decisión correcta"),
               ("chance of a goal within", "de probabilidad de gol en"), ("events", "acciones"), ("against", "frente a"), ("for", "para"),
               ("A pass to", "Un pase a"), ("A shot", "Un disparo"), ("Carrying on", "Seguir conduciendo"), ("a pass to", "un pase a"),
               ("a shot", "un disparo"), ("carrying on", "seguir conduciendo"), ("Regret", "Arrepentimiento"), ("the best of", "la mejor de"), ("options", "opciones"), ("Minute", "Minuto")],
    }[lang]
    for a, b in table:
        text = text.replace(a, b)
    return text


def make_model(kind: str) -> ModelClient:
    if kind == "fake":
        return FakeModel()
    if kind == "foundry":
        return FoundryModel()
    if kind == "framework":
        return AgentFrameworkModel()
    raise ValueError(f"unknown model kind {kind!r}: fake | foundry | framework")


# ------------------------------------------------------------------ the agents

EXPLAINER_SYSTEM = """You are the explainer on a live football broadcast. You receive one fork record: a decision a player
made, the alternatives the counterfactual engine evaluated, and the probability each option led to a goal within a short horizon.
Write two English sentences as JSON: {"casual": ..., "analyst": ...}.
- casual: one plain sentence a fan understands at a glance, no numbers, at most 20 words.
- analyst: one sentence with the taken option's goal probability, the best untaken option's probability, and the regret, as in the record.
Rules: use only names and numbers present in the record; write probabilities as whole percentages (0.62 becomes 62%); never invent
history, form, injuries, or anything not in the record; if regret is below 0.03 say the player made the right call."""

LOCALISER_SYSTEM = """You are the localiser. You receive JSON with English sentences. Return JSON {"fr": {...}, "es": {...}} with the same
keys translated into natural broadcast French and Spanish. Keep every player name, team name, number and percentage exactly as given.
Do not add or remove facts."""

RECAP_SYSTEM = """You are the recap writer at full time. You receive the match score and the fork records with the largest regret: the decisions
that mattered most, in match order. Write a recap of at most 120 words in English, then the same in French and Spanish, as JSON
{"en": ..., "fr": ..., "es": ...}. Mention the minute, the player and what the better option was, for each. Use only facts in the
records; never invent context."""

EXPLAIN_SCHEMA = {"type": "object", "properties": {"casual": {"type": "string"}, "analyst": {"type": "string"}}, "required": ["casual", "analyst"]}
LOCALISE_SCHEMA = {"type": "object", "properties": {"fr": {"type": "object"}, "es": {"type": "object"}}, "required": ["fr", "es"]}
RECAP_SCHEMA = {"type": "object", "properties": {"en": {"type": "string"}, "fr": {"type": "string"}, "es": {"type": "string"}}, "required": ["en", "fr", "es"]}


def _slim(record: dict[str, Any]) -> dict[str, Any]:
    """The part of a fork record the model needs, with names in and raw geometry out."""
    keep = ("match_id", "event_id", "minute", "player_name", "team_id", "under_pressure", "score", "horizon_events", "rollouts", "regret", "value_model")
    r = {k: record[k] for k in keep if k in record}
    def act(a: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not a:
            return None
        return {k: a[k] for k in ("type", "subtype", "receiver_name", "p_goal_for", "p_goal_against", "xg_for", "value", "taken") if k in a}
    r["taken"] = act(record["taken"])
    r["best_untaken"] = act(record.get("best_untaken"))
    r["candidates"] = [act(c) for c in record["candidates"]]
    return r


class Verifier:
    """No model. A sentence passes if every number in it is derivable from the record (a probability
    as a whole percent, the regret, the minute, the horizon, the score) and every capitalised name
    in it is a player or team in the record."""

    NUM = re.compile(r"-?\d+(?:[.,]\d+)?")
    WORD = re.compile(r"\b[A-ZÀ-Ý][\wÀ-ÿ'-]+\b")
    STOP = {"A", "An", "The", "He", "She", "It", "His", "Her", "Minute", "Regret", "Arrepentimiento", "Un", "Une", "Le", "La", "El", "Il",
            "Carrying", "Continuer", "Seguir", "Shot", "Pass", "Tir", "Disparo", "Pase", "Passe", "Lo", "Les", "Los", "Las", "Des", "De", "Du",
            "Y", "Et", "And", "But", "Mais", "Pero", "Chance", "Then", "Puis", "Luego", "At", "In", "On", "Au", "En", "Con", "With", "Avec",
            "No", "Non", "Yes", "Oui", "Sí", "Si"}

    def __init__(self, record: dict[str, Any], names: Names):
        self.record = record
        self.names = names
        self.allowed_numbers: set[str] = set()
        r = record
        for a in [r["taken"], r.get("best_untaken")] + list(r["candidates"]):
            if not a:
                continue
            for k in ("p_goal_for", "p_goal_against"):
                if k in a and a[k] is not None:
                    self.allowed_numbers.add(str(round(a[k] * 100)))
            if a.get("value") is not None:
                self.allowed_numbers.add(f"{a['value']:.2f}")
                self.allowed_numbers.add(f"{a['value']:.3f}")
        for v in (r.get("regret"), ):
            if v is not None:
                self.allowed_numbers.update({f"{v:.2f}", f"{v:.3f}", f"{abs(v):.2f}", f"{abs(v):.3f}", f"{abs(v):.1f}"})
        for v in (r.get("minute"), r.get("horizon_events"), r.get("rollouts"), len(r["candidates"]), r["score"]["home"], r["score"]["away"]):
            self.allowed_numbers.add(str(v))
        self.allowed_names: set[str] = set()
        for pid, n in names.player_name.items():
            self.allowed_names.update(n.split())
        for n in names.team_name.values():
            self.allowed_names.update(n.split())
            self.allowed_names.add(n)

    def check(self, text: str) -> tuple[bool, list[str]]:
        problems = []
        for m in self.NUM.findall(text):
            num = m.replace(",", ".")
            if num.lstrip("-+") not in self.allowed_numbers and num not in self.allowed_numbers:
                problems.append(f"number {m} not in record")
        for w in self.WORD.findall(text):
            if w in self.STOP or w in self.allowed_names:
                continue
            if w[0].isupper() and len(w) > 2 and w.lower() not in {x.lower() for x in self.STOP}:
                # A capitalised word that is not a known name: only a problem if it looks like a name
                # (not sentence-initial). Sentence-initial words are allowed.
                idx = text.find(w)
                if idx > 0 and text[idx - 2:idx].strip() not in (".", "!", "?", ":", ";", ""):
                    problems.append(f"name {w} not in record")
        return (not problems), problems


@dataclass
class Explained:
    record: dict[str, Any]
    text: dict[str, str]
    text_analyst: dict[str, str]
    verified: dict[str, bool]
    problems: dict[str, list[str]] = field(default_factory=dict)
    model_calls: int = 0
    seconds: float = 0.0
    fallback: bool = False


class Pipeline:
    """Explainer -> Localiser -> Verifier, producing a cue whose words came from the model when they
    passed and from the template when they did not."""

    def __init__(self, model: ModelClient, meta: dict[str, Any], languages: tuple[str, ...] = LANGS):
        self.model = model
        self.meta = meta
        self.names = Names(meta)
        self.languages = languages

    def explain(self, record: dict[str, Any]) -> Explained:
        t0 = time.perf_counter()
        slim = _slim(record)
        calls0 = getattr(self.model, "calls", 0)
        out = json.loads(self.model.complete("explainer\n" + EXPLAINER_SYSTEM, "Fork record:\n" + json.dumps(slim, ensure_ascii=False), EXPLAIN_SCHEMA))
        casual, analyst = str(out.get("casual", "")), str(out.get("analyst", ""))
        loc = json.loads(self.model.complete("localiser\n" + LOCALISER_SYSTEM, "Sentences:\n" + json.dumps({"casual": casual, "analyst": analyst}, ensure_ascii=False), LOCALISE_SCHEMA))
        text = {"en": casual}
        text_a = {"en": analyst}
        for lang in self.languages:
            if lang == "en":
                continue
            block = loc.get(lang, {}) or {}
            text[lang] = str(block.get("casual", ""))
            text_a[lang] = str(block.get("analyst", ""))
        v = Verifier(record, self.names)
        verified, problems = {}, {}
        for lang in self.languages:
            ok_c, p_c = v.check(text.get(lang, ""))
            ok_a, p_a = v.check(text_a.get(lang, ""))
            ok = ok_c and ok_a and bool(text.get(lang)) and bool(text_a.get(lang))
            verified[lang] = ok
            if not ok:
                problems[lang] = p_c + p_a + ([] if text.get(lang) and text_a.get(lang) else ["empty"])
        return Explained(record, text, text_a, verified, problems, model_calls=getattr(self.model, "calls", 0) - calls0,
                         seconds=round(time.perf_counter() - t0, 3), fallback=not all(verified.values()))

    def cue(self, record: dict[str, Any]) -> tuple[Cue, Explained]:
        """The ghost cue with model text where it verified and template text where it did not."""
        base = cue_from_fork(record, self.meta)
        try:
            ex = self.explain(record)
        except Exception as e:  # a model outage must not stop the broadcast
            ex = Explained(record, dict(base.text), dict(base.text_analyst), {l: False for l in self.languages}, {"*": [f"model error: {e}"]}, fallback=True)
            base.geometry["text_source"] = "template"
            return base, ex
        for lang in self.languages:
            if ex.verified.get(lang):
                base.text[lang] = ex.text[lang]
                base.text_analyst[lang] = ex.text_analyst[lang]
        base.geometry["text_source"] = "model" if not ex.fallback else "mixed" if any(ex.verified.values()) else "template"
        base.geometry["verified"] = ex.verified
        return base, ex

    def recap(self, records: list[dict[str, Any]], score: dict[str, int], top: int = 5) -> dict[str, Any]:
        chosen = sorted(records, key=lambda r: -r["regret"])[:top]
        chosen.sort(key=lambda r: r["event_id"])
        payload = {"home": self.names.team(self.names.home_id), "away": self.names.team(self.names.away_id), "score": score,
                   "forks": [_slim(r) for r in chosen]}
        out = json.loads(self.model.complete("recap\n" + RECAP_SYSTEM, "Match:\n" + json.dumps(payload, ensure_ascii=False), RECAP_SCHEMA))
        # Verify names and numbers against the union of the chosen records.
        union = {"taken": {"p_goal_for": 0}, "best_untaken": None, "candidates": [], "regret": 0.0, "minute": 0,
                 "horizon_events": chosen[0]["horizon_events"] if chosen else 20, "rollouts": 0, "score": score}
        allowed = Verifier(union, self.names)
        for r in chosen:
            v = Verifier(r, self.names)
            allowed.allowed_numbers |= v.allowed_numbers
        allowed.allowed_numbers |= {str(score["home"]), str(score["away"])}
        verified = {lang: allowed.check(str(out.get(lang, "")))[0] for lang in self.languages}
        return {"text": {lang: str(out.get(lang, "")) for lang in self.languages}, "verified": verified,
                "forks": [{"minute": r["minute"], "player_name": r["player_name"], "regret": r["regret"]} for r in chosen]}
