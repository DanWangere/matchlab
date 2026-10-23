"""Load the `matchlab export` files into Eventhouse reference tables with queued ingestion.

Usage:
    pip install azure-kusto-data azure-kusto-ingest
    az login
    python fabric/scripts/ingest_reference.py --ingest-uri https://ingest-<...>.kusto.fabric.microsoft.com \
        --database <kql database name> --export data/export

The query URI is on the Eventhouse's KQL database page ("Query URI"); the ingest URI is the same host
with an `ingest-` prefix. The "Get data" wizard in the Fabric portal does the same job by hand.
"""
from __future__ import annotations

import argparse
import os
import sys

TABLES = {
    "teams.jsonl": "Teams",
    "players.jsonl": "Players",
    "matches.jsonl": "Matches",
    "lineups.jsonl": "Lineups",
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ingest-uri", required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--export", default="data/export")
    p.add_argument("--also-truth", action="store_true", help="also load truth_minutes and truth_changes (tables TruthMinutes, TruthChanges must exist)")
    a = p.parse_args()

    from azure.kusto.data import KustoConnectionStringBuilder
    from azure.kusto.data.data_format import DataFormat
    from azure.kusto.ingest import IngestionProperties, QueuedIngestClient

    kcsb = KustoConnectionStringBuilder.with_az_cli_authentication(a.ingest_uri)
    client = QueuedIngestClient(kcsb)
    tables = dict(TABLES)
    if a.also_truth:
        tables.update({"truth_minutes.jsonl": "TruthMinutes", "truth_changes.jsonl": "TruthChanges"})
    for fname, table in tables.items():
        path = os.path.join(a.export, fname)
        if not os.path.exists(path):
            print(f"skip {fname}: not found", file=sys.stderr)
            continue
        props = IngestionProperties(database=a.database, table=table, data_format=DataFormat.MULTIJSON)
        client.ingest_from_file(path, ingestion_properties=props)
        print(f"queued {fname} -> {table}")
    print("queued ingestion takes a minute or two; check with: .show ingestion failures | top 5 by FailedOn desc")
    return 0


if __name__ == "__main__":
    sys.exit(main())
