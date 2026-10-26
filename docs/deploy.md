# Deploying matchlab

Two services, one image, on Azure Container Apps:

| App | Command | Ingress | Talks to |
| --- | --- | --- | --- |
| `matchlab-hub` | `matchlab serve` | public HTTPS (the overlay, `/stream`, `/state`, `/healthz`) | nothing; it is written to by forkd |
| `matchlab-forkd` | `matchlab forkd --source eventhub \| eventhouse ... --agents foundry` | none | the Eventstream custom endpoint or the Eventhouse, the Foundry model, the hub |

Fabric (Eventstream, Eventhouse, the dashboard) and the Foundry project are not created by the
template; `fabric/README.md` and `docs/agents.md` set those up. The template creates a user-assigned
identity that signs in to both without keys, plus a registry, a Log Analytics workspace and the
Container Apps environment. `infra/main.bicep` is the whole of it.

## What the hub is, and is not

The hub keeps the current match's metadata, cues and recent events in memory and fans them out
over server-sent events. One replica serves a studio and a few hundred viewers; it is not a CDN.
Its three write routes (`POST /events`, `/cues`, `/meta`) are guarded by `MATCHLAB_HUB_KEY`
(header `X-Hub-Key`); reads are open because the overlay is meant to be watched. To scale beyond
one replica, put Web PubSub behind the same three routes and Cosmos DB behind `/state`; nothing
upstream changes.

## The replay store

Replay needs a Cosmos DB for NoSQL account; the template does not create one because the free tier
(1000 RU/s and 25 GB, one account per subscription) is a choice made at account creation and cannot
be combined with serverless. Create it in the portal with Capacity mode *Provisioned throughput* and
*Apply Free Tier Discount*, then either let `infra/cosmos.bicep` create the database `matchlab` and
container `matches` (partition key `/match_id`, 400 RU/s) by passing `cosmosAccountName`, or make
them in Data Explorer yourself. The module also gives the services' identity the Cosmos DB Built-in
Data Contributor role; on a laptop, give your own login the same role once:

```bash
az cosmosdb sql role assignment create --account-name <account> --resource-group <rg> \
  --role-definition-id 00000000-0000-0000-0000-000000000002 \
  --principal-id $(az ad signed-in-user show --query id -o tsv) --scope "/"
export COSMOS_ENDPOINT='https://<account>.documents.azure.com:443/'
```

With `COSMOS_ENDPOINT` set, `forkd` records every match it follows (`--no-store` turns it off) and
the hub offers `GET /matches` and `POST /replay/<match_id>?speed=10`; the overlay shows the picker.
A replay never starts over a live match unless `force=1` is passed. `COSMOS_KEY` is accepted instead
of a role, for an emulator or a throwaway account.

## Deploy from GitHub Actions

1. **Resource group and OIDC.** Create an app registration (or a user-assigned identity) with a
   federated credential for `repo:<owner>/matchlab:ref:refs/heads/main` and
   `repo:<owner>/matchlab:environment:` (or whatever ref you run the workflow from), and give it
   Contributor and User Access Administrator on the resource group (the template assigns roles).
2. **Repository secrets:** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
   `MATCHLAB_HUB_KEY` (any long random string), `EVENTHUB_CONNECTION_STRING` and `EVENTHUB_NAME`
   (from the Eventstream custom endpoint **destination** `matchlab-out`, SAS Key Authentication; the
   fork service reads the stream, it does not send to it), `KUSTO_QUERY_URI`
   (the KQL database's Query URI) and, to write fork records back, `KUSTO_INGEST_URI` (the same host
   with `ingest-` in front).
3. **Repository variables:** `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`, `FOUNDRY_MODEL_ENDPOINT`
   (`https://<account>.services.ai.azure.com/openai/v1`), `FOUNDRY_DEPLOYMENT`,
   `FOUNDRY_ACCOUNT_NAME` and `FOUNDRY_RESOURCE_GROUP` (so the identity gets the Azure AI User role
   on the account; leave the name empty to assign it by hand), `COSMOS_ACCOUNT_NAME` and
   `COSMOS_RESOURCE_GROUP` for the replay store (leave empty for no replay).
4. **Run the `deploy` workflow** (Actions, deploy, Run workflow). It deploys the base resources,
   builds the image in the registry with `az acr build`, deploys the two apps, and waits for
   `/healthz`. The run summary prints the overlay URL and the identity's principal id.
5. **Give the identity access to Fabric.** In the Fabric workspace, Manage access, add the identity
   (search by the principal id or the name `id-matchlab`) as Contributor. The tenant setting
   "Service principals can use Fabric APIs" must be on for that workspace. Without this the
   Eventhouse source and `--ingest` fail with 401; the Event Hub source still works because it uses
   the connection string.
6. **Consumer group.** forkd reads the destination on `$Default` unless `eventHubConsumerGroup`
   names one you added in the destination's settings. Or deploy with `source: eventhouse` and a
   match id to replay from the table.

Then stream a match from anywhere:

```bash
matchlab stream data/matches/RAV-IRO-1000 --speed 10 --sink eventhub --batch 20 --live-ts
```

and open the overlay URL. Ghosts appear a few seconds after each fork-worthy decision; the words on
them are upgraded in place when the agent team's text verifies.

## Deploy from a laptop

The same two phases with the CLI:

```bash
az group create -n rg-matchlab -l swedencentral
export HUB_KEY=$(openssl rand -hex 24) EVENTHUB_CONNECTION_STRING='...'
az deployment group create -g rg-matchlab -f infra/main.bicep -p hubKey="$HUB_KEY"
REGISTRY=$(az deployment group show -g rg-matchlab -n main --query properties.outputs.registryLoginServer.value -o tsv)
az acr build --registry "${REGISTRY%%.*}" --image matchlab:local --file Dockerfile .
az deployment group create -g rg-matchlab -f infra/main.bicep -p infra/main.bicepparam -p imageTag=local
az deployment group show -g rg-matchlab -n main --query properties.outputs.hubUrl.value -o tsv
```

`infra/main.bicepparam` holds the non-secret values; edit it first. `az bicep build -f infra/main.bicep`
checks the template without deploying.

## Run the image locally

```bash
docker build -t matchlab .
docker run --rm -p 8080:8080 matchlab                                   # the hub
docker run --rm --env-file .env matchlab matchlab forkd --source eventhouse --match RAV-IRO-1000 \
    --hub http://host.docker.internal:8080 --mirror --agents off         # the fork service
```

The image carries the league, forty matches' metadata and the learned threat grid (`Dockerfile`
build args `LEAGUE_SEED`, `MATCHES`, `MATCH_SEED` must match what was streamed into the Eventhouse);
the hidden truth files are left out. Signed-in access from a container uses `AZURE_CLIENT_ID` /
`AZURE_TENANT_ID` / `AZURE_CLIENT_SECRET` for an app, or the managed identity in Azure.

## Cost and teardown

Basic registry, a consumption Container Apps environment with two single-replica apps (0.5 and
1 vCPU) and 30-day logs: a few dollars a day while running. `az group delete -n rg-matchlab` removes
all of it; Fabric and Foundry are untouched.
