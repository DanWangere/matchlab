# Step 5: the agent team on Microsoft Foundry

The ghost's words come from the model, and nothing reaches the screen that the record cannot back.

| Agent | Model? | Input | Output | Rule |
| --- | --- | --- | --- | --- |
| Explainer | yes | the fork record (names in, geometry out) | one casual sentence, one analyst sentence, English | only names and numbers from the record; probabilities as whole percent |
| Localiser | yes | the two English sentences | French and Spanish | names and numbers unchanged |
| Verifier | no | each sentence plus the record | pass or fail, with reasons | every number must be derivable from the record; every capitalised name must be a player or team in it |
| Recap | yes | the match's five highest-regret forks and the score | a recap in three languages | same rules, verified against the union of the records |

A sentence that fails verification is replaced by the template text from `cues.py`, per language, so
one bad translation does not cost the others. A model outage falls back to the template for all three.
The cue carries `geometry.text_source` (`model`, `mixed`, `template`) and `geometry.verified`, so
the renderer and the analytics can tell which words came from where.

## Running it

Prerequisites, once: `az login`, and your identity has Azure AI User on the Foundry resource. The
endpoint and deployment default to this project (`aif-garage-swedencentral`, `gpt-5.6-sol`); override
with `FOUNDRY_MODEL_ENDPOINT`, `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_DEPLOYMENT`.

```bash
python -m pip install openai azure-identity                 # the foundry model kind
python -m pip install agent-framework-openai                # optional: the framework model kind
matchlab explain data/matches/RAV-IRO-1000 --agents foundry           # one fork, all three stages printed
matchlab forkd --source file --match data/matches/RAV-IRO-1000 --agents foundry --hub http://localhost:8080 --mirror --out runs/forks.jsonl
matchlab recap runs/forks.jsonl --agents foundry                      # the full-time story
python evals/groundedness.py --records runs/forks.jsonl --foundry --upload   # score it in the Foundry project
```

`--agents framework` runs the same prompts through the Microsoft Agent Framework's `Agent` on an
`OpenAIChatClient`; `--agents foundry` uses the OpenAI client directly on the project's
`/openai/v1` endpoint; `--agents fake` needs no network and is what the tests use. All three go
through the same Verifier.

Latency: two model calls per shown fork (Explainer, Localiser). On the live path the ghost cue is
posted after both return, so a slow model delays the ghost, never the match; the Verifier is
microseconds. If the model takes longer than the window a ghost is useful in, run forkd with
`--agents off` for the live screen and `matchlab recap` for the full-time story.

## Evaluation

`evals/groundedness.py` scores the sentences the service produced. Locally it reports the Verifier
pass rate and analyst coverage (the analyst sentence states the taken option's probability and the
regret). With `--foundry` it also runs Azure AI Evaluation's `GroundednessEvaluator` and
`RelevanceEvaluator` with the fork record as context, judged by the deployed model; `--upload` logs
the run to the Foundry project, where it shows under Evaluation. The last summary lands in
`evals/last_run.json`.

## Why this shape

The brief's explainability criterion is "why a moment matters rather than that it happened". The
numbers for that come from the counterfactual engine, not the model; the model's job is to say them
well, in three languages, and the Verifier's job is to make sure it said only them. That split is
what lets a language model sit on a live feed.
