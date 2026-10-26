# Step 2: the stream into Fabric

Goal: a match replays into an Eventhouse through an Eventstream, and KQL functions turn the raw
events into match state (possession chains, threat, pass quality, speed metrics, windowed team
features, momentum, control versus chaos). Everything later (fork records, the ledger, cues) lands
in the same database.

What's in this folder:

| File | What it is |
| --- | --- |
| `eventhouse/01_tables.kql` | Tables, the JSON ingestion mapping, streaming-ingestion and retention policies |
| `eventhouse/02_functions.kql` | Stored functions: `MatchState`, `PossessionChains`, `LiveStats`, `ThreatGrid`, `MoveValue`, `PlayerValue`, `PassQuality`, `SpeedMetrics`, `PlayerThresholds`, `PlayerTag`, `TeamWindows`, `Momentum`, `ControlChaos`, `KeyMoments`, `FeatureShifts` |
| `eventhouse/03_queries.kql` | Pipeline checks, dashboard tiles, Activator starting points |
| `scripts/ingest_reference.py` | Optional: queued ingestion of the exported reference files |

Both scripts have been run against a live Eventhouse. Two things learned on the way, already
reflected in the files: the retention policy takes a JSON document, and `let` statements inside a
stored function body fail to parse in the queryset editor, so the functions use inline subqueries
and `toscalar` instead.

## 1. Eventhouse

1. In your workspace, create an **Eventhouse** (name it `matchlab`). It creates a KQL database of
   the same name.
2. Open the database, then **Explore your data** (a KQL queryset).
3. Paste `01_tables.kql` and run the commands **one at a time**: management commands (the ones
   starting with a dot) cannot share a query block.
4. Do the same with `02_functions.kql`. `.show functions` should list fifteen functions afterwards.

## 2. Reference data

```bash
matchlab league
matchlab generate --matches 40 --seed 1      # a bigger batch makes ThreatGrid smoother
matchlab export                              # data/export/*.jsonl
```

Load `teams.jsonl`, `players.jsonl`, `matches.jsonl` and `lineups.jsonl` into the tables of the
same names: in the database, **Get data > Local file**, pick the table, upload the file, format
JSON (each line is one object). Or, with `az login` done:

```bash
pip install azure-kusto-data azure-kusto-ingest
python fabric/scripts/ingest_reference.py --ingest-uri https://ingest-<host>.kusto.fabric.microsoft.com --database matchlab
```

The host is the one in the database's **Query URI** on its overview page.

The truth files (`data/truth/*.truth.json` and the flattened `truth_minutes.jsonl`,
`truth_changes.jsonl`, `truth_schedules.jsonl`) go to a **Lakehouse**, under `Files/matchlab/truth/`:
upload through the Lakehouse explorer, or OneLake file explorer on Windows. They are training and
scoring data for Undertow; they never go through the Eventstream. `--also-truth` on the script loads
the minute and change rows into Eventhouse too, which makes scoring a detection a KQL join.

## 3. Eventstream

1. Create an **Eventstream** named `matchlab-events`.
2. Add a source: **Custom endpoint**. Name it `matchlab-sim`. Publish.
3. Select the source node. Under **Details > Event Hub**, copy the **Connection string-primary key**
   and the **Event hub name**.
4. Add a destination: **Eventhouse**, mode **Direct ingestion**, workspace, Eventhouse `matchlab`,
   database `matchlab`. Save, then **Publish**. In direct-ingestion mode the table is chosen after
   publishing: click the destination node, then **Configure**, and a Get data wizard opens.
5. The wizard's Inspect step needs sample events, so start `matchlab stream` (section 4) first,
   then click **Retry**, pick table `MatchEvents`, format JSON, Finish.

The wizard creates its own mapping, `MatchEvents_mapping`, and infers column types from the sample.
On the live run it inferred `event_id`, `xg` and `related_event_id` differently from the table
(the first rows have integer-looking xG and null related ids), decided they conflicted, and added
`event_id_1`, `xg_1`, `related_event_id_1` columns. Fix, once, in the queryset:

```kusto
.create-or-alter table MatchEvents ingestion json mapping 'MatchEvents_mapping' '<the mapping JSON from 01_tables.kql>'
.drop table MatchEvents columns (event_id_1, xg_1, related_event_id_1)
.clear table MatchEvents data
```

then stream the match again. Events sent before the destination was configured are not stored.

The topology is `matchlab-sim -> matchlab-events -> MatchEvents`. If you later add an Event
Processor step (filtering, windowing in the stream), keep the direct path too; the raw table is
what the fork service and the renderer replay from.

## 4. Stream a match

```bash
pip install azure-eventhub
export EVENTHUB_CONNECTION_STRING='Endpoint=sb://...'     # from step 3.3
export EVENTHUB_NAME='es_...'                             # the event hub name, not the stream name
matchlab stream data/matches/RAV-IRO-1000 --speed 10 --sink eventhub --batch 20 --live-ts
```

`--speed 10` replays a match in about ten minutes; `--speed 60` in about a minute and a half.
`--live-ts` rewrites `ts` to now so the latency check in `03_queries.kql` means something.
`--batch 20` sends twenty events per Event Hub batch; one event per send also works, just slower.

On PowerShell: `$env:EVENTHUB_CONNECTION_STRING='...'` and `$env:EVENTHUB_NAME='...'`.

While it runs, in the queryset:

```kusto
MatchState("RAV-IRO-1000")
LiveStats("RAV-IRO-1000")
Momentum("RAV-IRO-1000") | render linechart
```

Rows should appear within a few seconds of being sent (streaming ingestion is enabled on the
table). If nothing arrives after a minute, check `.show ingestion failures` and the Eventstream's
destination node for mapping errors; a `ts` that is not ISO 8601 and a `detail` that is not an
object are the usual causes.

## 5. Studio desk (Real-Time Dashboard)

Create a **Real-Time Dashboard** from the database and add a parameter `match_id` (text, single
value, default `RAV-IRO-1000`). Each tile is one query from `03_queries.kql` with the literal id
replaced by the bare parameter name `match_id` (no braces: a dashboard parameter is a KQL variable). A first page:

- Ticker: `LiveStats(match_id)`
- Momentum: `Momentum(match_id) | project minute, momentum` as a line chart
- Key moments: `KeyMoments(match_id) | top 8 by importance desc`
- Pressure per five minutes: `TeamWindows(match_id, 300.0) | project minute, team_id, pressures_made`
- Control versus chaos: `ControlChaos(match_id, 300.0)`
- Pass quality and speed leaders: `PassQuality(match_id) | top 10 by hard_passes desc`, `SpeedMetrics(match_id) | top 5 by fastest_shot_kmh desc`
- Live feed: the last-20-events query

Set auto refresh to the shortest interval the capacity allows.

## 6. Activator starting points

Activator works on a query over the Eventhouse or directly on the Eventstream. Two rules to start:

- Momentum below -0.5 for two consecutive minutes (the query is in `03_queries.kql`): fire a Teams
  message now; later, fire the Undertow attributor.
- A shot with xG at least 0.3 in the last minute: later, fire the Otherwise fork service.

## 7. The fork service against Fabric (step 4)

`matchlab forkd` is the Otherwise service. It needs the match metadata (from `data/matches/` or the
`Matches` and `Lineups` tables) and the league file; it learns the threat grid from the matches in
`data/` once and caches it in `data/threat_grid.json`.

Live, tailing the same Event Hub the Eventstream reads (the two environment variables from section 4):

```bash
python -m pip install azure-eventhub
matchlab serve                                                   # terminal 1: the renderer at :8080
matchlab forkd --source eventhub --hub http://localhost:8080 --mirror   # terminal 2: follow and fork
matchlab stream data/matches/RAV-IRO-1000 --speed 10 --sink eventhub --batch 20 --live-ts   # terminal 3
```

`--mirror` forwards the events it reads to the renderer, so the overlay and the ghosts come from one
process. The Eventhouse keeps filling from the Eventstream in parallel. A second consumer on the same
Event Hub needs its own consumer group if the Eventstream's reader is already using `$Default`; add
one in the custom endpoint source's settings and pass `--consumer-group <name>`.

Replaying a match already in Eventhouse (no Event Hub needed):

```bash
python -m pip install azure-kusto-data azure-kusto-ingest
az login
export KUSTO_QUERY_URI='https://<host>.kusto.fabric.microsoft.com'     # the database's Query URI
export KUSTO_INGEST_URI='https://ingest-<host>.kusto.fabric.microsoft.com'
export KUSTO_DATABASE=matchlab
matchlab forkd --source eventhouse --match RAV-IRO-1000 --hub http://localhost:8080 --mirror --ingest
```

`--ingest` writes every fork to `ForkRecords` and every ghost to `Cues` by queued ingestion (a minute
or two to appear). `--follow` keeps polling for new rows, which makes the Eventhouse path work for a
live match too, at a few seconds more latency than the Event Hub path. Check:

```kusto
ForkRecords | summarize forks = count(), avg_regret = round(avg(regret), 3), worth_it = countif(regret >= 0.03) by match_id
ForkRecords | where match_id == "RAV-IRO-1000" | top 5 by regret desc | project minute, player_name, taken = taken.type, best = best_untaken.type, regret
```

For a service principal instead of `az login`, set `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and
`AZURE_CLIENT_SECRET`; the app needs Viewer on the database for queries and Ingestor for `--ingest`.

## What this gives the next steps

- Otherwise (`matchlab forkd`) follows the stream, values branches with the threat grid, and writes `ForkRecords` and `Cues`.
- Undertow reads `TeamWindows` and `FeatureShifts`; it writes `LedgerRows`; `TruthMinutes` and
  `TruthChanges` score it.
- The renderer reads `Cues` (mirrored from Cosmos DB) and `PlayerTag` for name tags.
- A Fabric data agent over this database answers studio questions ("who completed the hardest
  passes in the second half?") with no extra work.
