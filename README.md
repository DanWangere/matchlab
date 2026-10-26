# matchlab

A synthetic football league with hidden ground truth and a deterministic, rewindable event
simulator. It is the base layer of the match-intelligence project: everything downstream
(Fabric ingestion, the Otherwise fork engine, the Undertow hidden-state estimator, the overlay
renderer, the agents) consumes what this package produces.

All clubs, players and matches are fictional. No real match data is included or required.

## What it produces

| Output | Where | Streamed? | Used by |
| --- | --- | --- | --- |
| Event stream, one row per on-ball action | `data/matches/<id>.events.jsonl` | yes | Eventstream, Eventhouse, renderer, agents |
| Match metadata: line-ups, formations, venue | `data/matches/<id>.meta.json` | at kick-off | everything |
| Hidden ground truth: momentum, fatigue, press intent, risk appetite per team per minute, plus every instruction change and its cause | `data/truth/<id>.truth.json` | never | Undertow training and scoring |
| Fork record: the decision taken, the alternatives, their rollout values | `matchlab fork --out` | as a ghost cue (`--cue`, `--post`) | Otherwise |
| Cues: timed overlay instructions in three languages | `matchlab play`, `cues.py` | yes | the renderer, any graphics partner |
| JSON schema for the event | `data/event.schema.json` | | Eventhouse table definition |
| Flat reference and truth files | `data/export/*.jsonl` (`matchlab export`) | | Eventhouse reference tables, Lakehouse |

Coordinates are metres on a 105 x 68 pitch, always in the acting team's attack frame
(towards x = 105). A renderer flips the away team's events.

## Run it

```bash
pip install -e ".[dev]"          # stdlib only; pytest for the tests, azure-eventhub optional
matchlab league --seed 7                                   # 20 clubs, 360 players
matchlab generate --matches 20 --seed 1                    # matches + truth into data/
matchlab stats data/matches/RAV-IRO-1000                   # one match's numbers
matchlab stream data/matches/RAV-IRO-1000 --speed 20       # replay to stdout at 20x
matchlab fork data/matches/RAV-IRO-1000 --rollouts 200     # price the biggest chance's alternatives
matchlab export                                            # flat JSONL for Eventhouse and Lakehouse
matchlab play data/matches/RAV-IRO-1000 --speed 20         # the overlay at http://localhost:8080 with baseline and fork cues
matchlab forkd --source file --match data/matches/RAV-IRO-1000 --hub http://localhost:8080 --mirror   # Otherwise, live
python -m pytest
```

## The overlay

`matchlab play` serves `renderer/index.html` and replays a match into it over server-sent events,
emitting cues on the match clock: the baseline cues computed from the stream (goals, big chances,
hard passes, fastest shot, cards, substitutions, half and full time, a momentum dial every minute,
player milestones) and ghost cues from forking the highest-xG shots before kick-off.

The page draws a 2D pitch with both line-ups, the ball, the current pass or carry, name tags, the
ghost path for a fork, a lower third, and a desk with the live cue stack, momentum, match stats and
a feed. Four viewer modes read the same cues differently: Analyst (numbers, every cue), Casual
(plain lines, the big moments), Club (one side's cues) and Player (one player's cues, a persistent
tag, and their running numbers). Three languages, switchable live: English, French, Spanish.

Everything on screen comes from cues, never from raw events, so a broadcast graphics partner can
consume the same objects (`matchlab/cues.py` documents the fields). `matchlab serve` runs the hub
alone; feed it with `matchlab stream --sink http://localhost:8080/events` and
`matchlab fork ... --post http://localhost:8080`, or POST to `/events`, `/cues` and `/meta` from
anywhere. The production shape is the same with Cosmos DB and Web PubSub in place of the hub.


Streaming to a Fabric Eventstream: create a custom-endpoint source, copy its Event Hub
connection string and name, then

```bash
pip install azure-eventhub
export EVENTHUB_CONNECTION_STRING='...'; export EVENTHUB_NAME='...'
matchlab stream data/matches/RAV-IRO-1000 --speed 10 --sink eventhub --batch 20 --live-ts
```

`--sink http://localhost:8080/events` posts JSON arrays to a local renderer or an Azure Function.

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
  own policy.
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

## Event schema notes

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

## Where this fits, and what comes next

1. **This package.** Generator, schema, stream, fork demo. Done.
2. **Fabric.** Scripts and the setup guide are in [`fabric/`](fabric/README.md): Eventstream
   custom-endpoint source fed by `matchlab stream`; Eventhouse tables for events, reference data,
   fork records, ledger rows and cues; KQL functions for match state (possession chains, a threat
   grid learned from the league, pass quality, speed metrics, windowed team features, momentum,
   control versus chaos); Lakehouse for the truth files.
3. **Cue contract and renderer.** Done locally: `cues.py`, `serve.py`, `renderer/index.html`,
   `matchlab play`. Remaining for the cloud: Cosmos DB for cues and viewer profiles, Web PubSub
   fan-out, the renderer hosted on Static Web Apps or Container Apps.
4. **Otherwise.** Done: `matchlab forkd` (`follow.py`, `threat.py`, `forkd.py`). A Follower keeps a
   deterministic copy of the match in step with the stream and forks just before each decision the
   policy wants (every open-play shot, final-third moments with a through ball, cross or shot
   available, at most one every 45 s). Branches are valued with a threat grid learned from the
   league (the same 12 x 8 cells as the Eventhouse `ThreatGrid`), so candidates separate after
   about 100 rollouts, 0.5 s per fork. Inputs: Event Hub (live), Eventhouse (replay or `--follow`),
   file. Outputs: ghost cues to the hub, JSONL, `ForkRecords` and `Cues` in Eventhouse.
5. **Foundry agents.** Explainer, Localiser (English, French, Spanish), Verifier, Recap, writing
   only from fork records and ledger rows.
6. **Undertow.** Pre-match ledger agent, windowed features in Eventstream, a state estimator
   scored against the truth files, KQL anomaly detection, Activator rules, Attributor agent,
   Real-Time Dashboard for the studio desk.
7. **Deploy and document.** Container Apps and Functions, README, demo video.
