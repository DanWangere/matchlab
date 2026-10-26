# matchlab

**Otherwise: the match that didn't happen, drawn on the one that did.**

A live football broadcast tells you what a player did. This shows what else they could have done,
and what it would have been worth. A striker shoots from a tight angle; a ghost path appears on the
pitch showing the square ball to the unmarked team-mate, with the chance of a goal within the next
twenty events for each option, in English, French or Spanish, a few seconds after the shot. At
full time, a recap of the five forks that decided the match.

Entry for the Microsoft x Premier League *Inside the Game* developer hackathon. Everything here is
synthetic: twenty fictional clubs, 360 fictional players, and a simulator that generates the
matches. No real match data is included or required.

## How it works, in one paragraph

The event stream comes from a **deterministic, rewindable simulator**. Because the same seed always
produces the same match, a second copy of the simulator can **follow the live stream in exact
step** and, just before a fork-worthy decision (every open-play shot, a final-third moment with a
cross, through ball or shot available), **snapshot the match, force each alternative, roll it out
a few hundred times, and restore**. Each branch is valued with a **threat grid learned from the
league** (the chance that possession in each of 12 x 8 pitch cells becomes a goal), so a
counterfactual is priced in half a second. The result is a *fork record*: the action taken, the
best untaken one, the regret between them, and each candidate's probability under the team's own
policy. A **Foundry agent team** turns the record into words: Explainer and Localiser write the
three languages in one structured call, a deterministic **Verifier** rejects any number or name that
is not in the record, and template text stands in for anything that fails. The overlay draws the
ghost immediately from the template and **upgrades the words in place** when the model's text
verifies. Fabric carries the stream (Eventstream to Eventhouse), the match state (fifteen KQL
functions) and the studio dashboard; fork records and cues are written back to the same Eventhouse.

## Architecture

```
 laptop / any producer                      Microsoft Fabric                              viewers
 ┌──────────────────┐   Event Hub     ┌──────────────┐   direct ingest   ┌─────────────────────┐
 │ matchlab stream  │ ─────────────▶  │ Eventstream  │ ────────────────▶ │ Eventhouse          │
 │ (or a real feed) │   custom        │ matchlab-    │                   │ MatchEvents, Teams, │
 └──────────────────┘   endpoint      │ events       │                   │ Players, Lineups,   │
                                      └──────┬───────┘                   │ Matches, ThreatGrid │
                                             │ custom endpoint           │ ForkRecords, Cues … │
                                             ▼ destination "matchlab-out"│ 15 KQL functions    │
 ┌────────────────────────────────────────────────────────────┐          │ Real-Time Dashboard │
 │ matchlab forkd  (Azure Container Apps)                     │ ◀──────▶ │ "matchlab-studio"   │
 │  Follower: deterministic twin of the match                 │ replay / └─────────────────────┘
 │  ForkPolicy → snapshot, rollout alternatives, restore      │ ingest
 │  ThreatGrid value → fork record (taken, best untaken,      │
 │                     regret, candidates)                    │          ┌─────────────────────┐
 │  Agents: Explainer+Localiser → Verifier → cue (EN/FR/ES)   │ ◀──────▶ │ Microsoft Foundry   │
 │          Recap at full time                                │ Entra ID │ gpt-5.6-sol         │
 └──────┬────────────────────┬────────────────────────────────┘          │ + evaluations       │
        │ meta, events per   │ POST /events /cues /meta  (X-Hub-Key)     └─────────────────────┘
        │ minute, final cues ▼
        │          ┌────────────────────────────────────────────────────────────┐
        │          │ matchlab serve  (Azure Container Apps, public)             │  SSE /stream   ┌──────────────┐
        ▼          │  cue hub: match state in memory, fan-out over SSE          │ ─────────────▶ │ renderer/    │
 ┌──────────────┐  │  renderer/index.html: 2D pitch, ghosts, lower thirds,      │                │ index.html   │
 │ Cosmos DB    │  │  Analyst / Casual / Club / Player modes, EN / FR / ES       │                │ (browser)    │
 │ matches/     │◀─│  POST /replay/<match_id>: the stored match, paced out again │                │  "Watch a    │
 │ /match_id    │  └────────────────────────────────────────────────────────────┘                │   match      │
 └──────────────┘                                                                                │   again"     │
                                                                                                 └──────────────┘
```

The hidden ground truth of the simulator (momentum, fatigue, press intent, risk appetite per team per
minute, and every instruction change with its cause) is never streamed; it is exported to the
Lakehouse for the second engine, Undertow, which is designed but not built in this entry.

## Run it

```bash
pip install -e ".[dev]"           # the core is stdlib only; extras: eventhub, fabric, agents, cosmos
matchlab league --seed 7                                   # 20 clubs, 360 players
matchlab generate --matches 40 --seed 1                    # matches + hidden truth into data/
matchlab play data/matches/RAV-IRO-1000 --speed 20         # the overlay at http://localhost:8080, forks precomputed
python -m pytest                                           # 36 tests
```

The live shape, three terminals (`.env` holds the Event Hub keys, the Eventhouse URIs and the hub
key; it is gitignored). `stream` sends to the Eventstream's custom endpoint source; `forkd` reads the
stream back through a custom endpoint destination (`EVENTHUB_READ_*`), in parallel with the
Eventhouse ingestion:

```bash
matchlab serve                                                                   # 1. the hub + overlay
matchlab forkd --source eventhub --hub http://localhost:8080 --mirror \
    --agents foundry --out runs/forks.jsonl                                      # 2. Otherwise, live
matchlab stream data/matches/RAV-IRO-1000 --speed 10 --sink eventhub --batch 20 --live-ts   # 3. the match
```

`--source eventhouse --match RAV-IRO-1000 [--follow]` replays a match already in the Eventhouse
instead of reading the Event Hub; `--source file` needs neither. `--agents off` draws ghosts with
template text only; `--agents fake` exercises the pipeline without Foundry. `--ingest` writes fork
records and cues to the Eventhouse `ForkRecords` and `Cues` tables.

**Replay for the viewer.** With `COSMOS_ENDPOINT` set (and `az login` done), `forkd` also records
the match in Cosmos DB as it runs: the metadata, the events chunked per minute, and every cue in its
final, verified wording. The overlay then offers "Watch a match again": the hub reads the match
back and paces it out to everyone connected, ghosts where they were, through the same stream. From
the terminal: `matchlab replay --list`, `matchlab replay RAV-IRO-1000 --speed 10`, or without
Cosmos, `matchlab replay --source file data/matches/RAV-IRO-1000 --forks runs/forks.jsonl`.

Other commands: `stats` (one match's numbers), `stream` (to stdout, a file, HTTP or Event Hub),
`fork` (price one decision's alternatives), `explain` (one fork through the agent team),
`recap` (the full-time story), `export` (flat JSONL for Eventhouse and Lakehouse).

- Fabric setup, one command at a time: [`fabric/README.md`](fabric/README.md)
- The agent team, prompts, verifier and evaluations: [`docs/agents.md`](docs/agents.md)
- Container Apps deployment, Bicep and the GitHub Actions workflow: [`docs/deploy.md`](docs/deploy.md)

## The overlay

`renderer/index.html` draws a 2D pitch with both line-ups, the ball, the current pass or carry,
name tags, the ghost path for a fork, a lower third, and a desk with the live cue stack, momentum,
match stats and a feed. Four viewer modes read the same cues differently: Analyst (numbers, every
cue), Casual (plain lines, the big moments), Club (one side's cues) and Player (one player's cues, a
persistent tag, and their running numbers). Three languages, switchable live.

Everything on screen comes from cues, never from raw events, so a broadcast graphics partner can
consume the same objects. A cue has a layer (`tag`, `card`, `lower_third`, `ghost`, `dial`,
`ledger`), text per language in two registers, geometry, the modes it belongs to, a priority and a
window on the match clock (`matchlab/cues.py`). A ghost cue arrives twice: first with template text
the moment the fork is priced, then again under the same `cue_id` with the agents' verified words;
the overlay marks model text with a diamond and the fork's value model and verification state ride
in the geometry.

The baseline cues (goals, big chances, hard passes, fastest shot, cards, substitutions, half and
full time, a momentum dial every minute, player milestones) are computed from the stream with no
model at all.

## Otherwise, in detail

- **Follower** (`follow.py`): keeps a simulator in step with the stream. Each incoming event is
  compared with the twin's own next event (type, team, position); a mismatch is counted and reported,
  never silently corrected. Across a full match the count is zero. The follower's policy decides when to fork: every open-play shot,
  final-third moments with a through ball, cross or shot among the candidates, at most one every
  45 seconds and 25 per match.
- **Fork** (`fork.py`): `snapshot()`, then for each candidate action `rollout(horizon, action, salt)`
  200 times (120 in the live service), then `restore()`. Candidates are the simulator's own
  options with the probabilities its policy would have given them, so "the pass he didn't see" is a
  real option with a real likelihood, not a hindsight invention.
- **Value** (`threat.py`): goals within the horizon are too rare to separate options, so each
  rollout is scored as goal difference plus half the xG plus 0.6 times the threat the possession
  reached, where threat is the learned 12 x 8 grid of goal probability per cell. The same grid
  lives in the Eventhouse as `ThreatGrid`, learned from the same matches in KQL. Candidates separate
  after about 100 rollouts; a fork takes about half a second on one core.
- **Regret** is the value of the best untaken option minus the taken one. Ghosts are posted when
  regret is at least 0.03, and for every shot regardless, in a finer line when the shot was right.
- **Replay** (`replay.py`): the fork service is the writer, so the replay store holds exactly what
  was shown, with a cue upgraded in place stored once under its `cue_id`. A replay is one partition
  read (`/match_id`) and a pacer that re-emits events and cues on the match clock; the baseline
  cues are recomputed from the events because they are deterministic. The Eventhouse keeps the
  analytics; Cosmos DB serves viewers.
- **Agents** (`agents.py`): one structured call writes the analyst and casual lines in three
  languages from a slimmed record whose percentages and regret are pre-rendered as strings, so the
  model copies rather than computes. The Verifier accepts a number only if it is a rendering of a
  value in the record (half-up, truncated, one decimal, signed hundredths) and a capitalised name
  only if it belongs to a player or club in the match. Anything else falls back to the
  template, per language. `evals/groundedness.py` scores a run locally and, with Azure AI
  Evaluation, uploads groundedness and relevance to the Foundry project.

## How the simulator works

One match is a `Simulator`. Each `step()` is `prepare()` (clock tick, pending restart,
pressure check) followed by `act()` (the carrier's action, sampled from the policy or forced).
The policy is `candidate_actions()`: concrete options (short passes to named team-mates, a
long ball, a through ball, a cross, a carry, a shot, a clearance), each with its probability
under the current zone, score state, pressure and the team's hidden risk appetite.

Three properties matter for the rest of the project:

- **Deterministic.** Same league, fixture and seed, same events. `fork.rebuild_to_event`
  re-simulates to the moment before any open-play action in milliseconds.
- **Rewindable.** `snapshot()` and `restore()` capture everything, including the random
  generator. `rollout(n, first_action, salt)` forces an action, continues for `n` events,
  and puts the match back exactly where it was. This is the simulator Otherwise borrows:
  no learned event model is needed to get a counterfactual that is exact under the league's
  own policy. With a real feed, a learned next-event model would take its place behind the same
  `snapshot` / `rollout` / `restore` interface.
- **Hidden state.** Press intent and risk appetite follow a pre-match schedule with planned
  shifts; substitutions and score state change them; momentum reacts to goals and drifts;
  fatigue rises with minutes and press intensity. They change pass selection, completion,
  pressure rate, duel outcomes and conversion, and they are logged per minute with every
  cause. Undertow is trained and scored against this log; it never sees it live.

## Calibration

Averages over 150 simulated matches, against the ranges a real top-flight match lands in:

| Measure | matchlab | Reference |
| --- | --- | --- |
| Passes per match | 946 | 850 to 1050 |
| Pass completion | 81% | 80 to 86% |
| Average pass length | 17.4 m | 17 to 21 m |
| Shots per match | 24 | 22 to 28 |
| On target | 9.2 | 7 to 10 |
| xG per match | 2.66 | 2.3 to 3.0 |
| Goals per match | 2.81 | 2.5 to 3.0 |
| Home goals vs away | 1.53 vs 1.27 | about 1.55 vs 1.2 |
| Pressure events | 357 | 250 to 400 |
| Tackles | 29 | 30 to 45 |
| Fouls | 16 | 18 to 26 |

The knobs are at the top of `sim.py` (`BASE_WEIGHTS`, `TYPE_ADJ`, `HOME_EDGE`) and in the
`_completion`, `_shot` and `_maybe_pressure` formulas. To calibrate against a real match-level
dataset, compare its per-match averages with `matchlab stats` across a batch and adjust
there; the real data stays out of the repo.

## Event schema

One row per on-ball action: `match_id, event_id, period, clock_s, ts, team_id, player_id, type,
subtype, x, y, end_x, end_y, outcome, under_pressure, pass_length_m, pass_difficulty,
ball_speed_mps, xg, possession_id, phase, related_event_id, score_home, score_away, detail`
(`data/event.schema.json`). Coordinates are metres on a 105 x 68 pitch, always in the acting
team's attack frame (towards x = 105); a renderer flips the away team's events.

- `type`: pass, carry, shot, tackle, interception, clearance, pressure, foul,
  possession_change, substitution, card, kick_off, period_end.
- Restarts are passes with subtype `goal_kick`, `corner`, `free_kick`, `throw_in`;
  penalties are shots with subtype `penalty`.
- `possession_change.detail.cause`: interception, recovery, tackle, out_of_play, clearance,
  save, block, goal.
- `pass_difficulty` is situational (length, type, pressure, zone), independent of the passer,
  so it is the brief's pass-quality rating; `ball_speed_mps` covers the speed metrics.
- `detail` carries extras: substitution on/off, card colour, period number.
- Period 2 restarts `clock_s` at 2700; `ts` is wall clock for live replay.

## Repository map

| Path | What |
| --- | --- |
| `matchlab/schema.py`, `league.py`, `truth.py`, `sim.py` | event contract, fictional league, hidden schedules, the simulator |
| `matchlab/fork.py`, `threat.py`, `follow.py`, `forkd.py` | Otherwise: fork, value, follower, the service and its inputs and outputs |
| `matchlab/cues.py`, `serve.py`, `renderer/index.html` | cue contract, hub, overlay |
| `matchlab/replay.py` | the Cosmos DB replay store, the per-minute event writer, the pacer |
| `matchlab/agents.py`, `docs/agents.md`, `evals/` | the agent team on Foundry, its verifier and evaluations |
| `matchlab/sinks.py`, `export.py`, `stats.py`, `cli.py` | outputs, exports, numbers, the command line |
| `fabric/` | KQL tables, functions and queries; reference ingestion; the Fabric setup guide |
| `infra/`, `Dockerfile`, `.github/workflows/` | Container Apps deployment, tests and image build on every push |

## Status

Built and run end to end: generator, stream into Fabric (Eventstream, Eventhouse, dashboard),
the fork service on a live Event Hub and on Eventhouse replay, the overlay with ghosts, the agent
team on gpt-5.6-sol through the Foundry project with verification and evaluation, replay from Cosmos
DB, Container Apps deployment. Deferred: **Undertow**, the hidden-state estimator with a pre-match assumption ledger,
whose training data (the truth files) and KQL feature functions are already here.
