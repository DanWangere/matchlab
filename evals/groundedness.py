"""Score the agent team's explanations against the fork records they came from.

Two scorers, run on the same rows:

* local: the deterministic Verifier (every number and name must be in the record), plus a
  coverage check (the analyst line mentions the taken option's probability and the regret).
* foundry: Azure AI Evaluation's GroundednessEvaluator, judged by the deployed model against
  the fork record as context. Needs `pip install azure-ai-evaluation` and `az login`.

Usage:
    python evals/groundedness.py --records runs/forks.jsonl                    # local only
    python evals/groundedness.py --records runs/forks.jsonl --foundry          # both
    python evals/groundedness.py --records runs/forks.jsonl --foundry --upload # also log the run to the Foundry project

The records file is the --out of `matchlab forkd --agents ...`; each line carries the record and
the explanation the model produced for it. Output: a table per scorer and a JSON summary.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from matchlab.agents import DEPLOYMENT, MODEL_ENDPOINT, PROJECT_ENDPOINT, Verifier, _slim  # noqa: E402
from matchlab.cues import Names  # noqa: E402


def load_rows(path: str, data_dir: str) -> list[dict]:
    rows = []
    metas: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            rec = d.get("record", d)
            ex = rec.get("explanation") or d.get("explanation")
            if not ex:
                continue
            mid = rec["match_id"]
            if mid not in metas:
                with open(os.path.join(data_dir, "matches", f"{mid}.meta.json"), encoding="utf-8") as mf:
                    metas[mid] = json.load(mf)
            for lang in ("en", "fr", "es"):
                for kind in ("text", "text_analyst"):
                    t = (ex.get(kind) or {}).get(lang)
                    if t:
                        rows.append({"match_id": mid, "event_id": rec["event_id"], "lang": lang, "kind": kind, "response": t,
                                     "context": json.dumps(_slim(rec), ensure_ascii=False), "record": rec, "meta": metas[mid],
                                     "query": "Explain why this decision mattered, using only the fork record."})
    return rows


def local_scores(rows: list[dict]) -> dict:
    out = {"rows": 0, "verified": 0, "coverage": 0}
    for r in rows:
        out["rows"] += 1
        v = Verifier(r["record"], Names(r["meta"]))
        ok, _ = v.check(r["response"])
        out["verified"] += 1 if ok else 0
        if r["kind"] == "text_analyst":
            p = str(round(r["record"]["taken"]["p_goal_for"] * 100))
            reg = f"{abs(r['record']['regret']):.2f}"
            out["coverage"] += 1 if (p in r["response"] and reg in r["response"]) else 0
    n_analyst = sum(1 for r in rows if r["kind"] == "text_analyst")
    out["verified_rate"] = round(out["verified"] / max(1, out["rows"]), 3)
    out["coverage_rate"] = round(out["coverage"] / max(1, n_analyst), 3)
    return out


def foundry_scores(rows: list[dict], upload: bool) -> dict:
    from azure.ai.evaluation import GroundednessEvaluator, RelevanceEvaluator, evaluate
    from azure.identity import DefaultAzureCredential

    model_config = {"azure_endpoint": MODEL_ENDPOINT.replace("/openai/v1", ""), "azure_deployment": DEPLOYMENT}
    cred = DefaultAzureCredential()
    ground = GroundednessEvaluator(model_config=model_config, credential=cred)
    relev = RelevanceEvaluator(model_config=model_config, credential=cred)
    path = "evals/_rows.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"query": r["query"], "context": r["context"], "response": r["response"]}, ensure_ascii=False) + "\n")
    kwargs = {}
    if upload:
        kwargs["azure_ai_project"] = PROJECT_ENDPOINT
    result = evaluate(data=path, evaluators={"groundedness": ground, "relevance": relev}, evaluation_name="matchlab-explanations", **kwargs)
    metrics = result.get("metrics", {})
    return {"rows": len(rows), "metrics": metrics, "studio_url": result.get("studio_url")}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--records", required=True)
    p.add_argument("--data", default="data")
    p.add_argument("--foundry", action="store_true")
    p.add_argument("--upload", action="store_true", help="log the evaluation run to the Foundry project")
    p.add_argument("--limit", type=int, default=0, help="evaluate only the first N rows (Foundry calls cost time)")
    a = p.parse_args()
    rows = load_rows(a.records, a.data)
    if a.limit:
        rows = rows[: a.limit]
    if not rows:
        print("no explanations in the records file; run forkd with --agents first", file=sys.stderr)
        return 1
    summary = {"local": local_scores(rows)}
    print(f"local verifier: {summary['local']['verified']}/{summary['local']['rows']} sentences verified "
          f"({summary['local']['verified_rate']:.0%}); analyst coverage {summary['local']['coverage_rate']:.0%}")
    if a.foundry:
        summary["foundry"] = foundry_scores(rows, a.upload)
        print("foundry:", json.dumps(summary["foundry"]["metrics"], indent=1))
        if summary["foundry"].get("studio_url"):
            print("run:", summary["foundry"]["studio_url"])
    with open("evals/last_run.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
