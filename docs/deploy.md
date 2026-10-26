# Deploying matchlab

Two services, one image, on Azure Container Apps, shaped so that nothing costs anything while no
match is on:

| Piece | Runs as | When it runs | Talks to |
| --- | --- | --- | --- |
| `matchlab-hub` | Container App, `matchlab serve`, public HTTPS | scales to zero after ~5 idle minutes; the first request wakes it in a few seconds | written to by the fork job; read by viewers over SSE; Cosmos DB for replays |
| `matchlab-forkd` | Container Apps **job**, `matchlab forkd ... --one-match` | one execution per match, started by hand, by the `matchday` workflow or by a cron; exits at full time | the Eventstream destination or the Eventhouse, the Foundry model, the hub, Cosmos DB |

`infra/main.bicep` is the whole template: the environment, a Log Analytics workspace, one
user-assigned identity, the hub app and the fork job. It references, but does not create, the Fabric
items (`fabric/README.md`), the Foundry project (`docs/agents.md`) and the Cosmos DB account (below).
The image lives on GitHub's registry by default, so there is no Azure registry to pay for; set the
repository variable `CONTAINER_REGISTRY=acr` to build into an Azure Container Registry instead.

## What it costs

Consumption pricing is per vCPU-second and GiB-second while a replica exists, with a monthly free
grant of 180,000 vCPU-seconds and 360,000 GiB-seconds per subscription, and no charge for the
environment itself.

| | |
| --- | --- |
| hub awake (0.5 vCPU, 1 GiB) | about $0.05 an hour, idle rate lower |
| hub asleep | $0 |
| fork job, one real-time match (1 vCPU, 2 GiB, ~100 min) | about $0.18 |
| fork job, a match streamed at 10x (~10 min) | about $0.02 |
| Log Analytics, Cosmos DB free tier, GitHub registry (public image) | $0 |
| Azure Container Registry, if chosen | about $5 a month, the only standing charge |

A demo week stays inside the free grant. `az group delete -n <rg>` removes the Container Apps
pieces; Fabric, Foundry and Cosmos DB are untouched.

## What the hub is, and is not

The hub keeps the current match's metadata, cues and recent events in memory and fans them out
over server-sent events. One replica serves a studio and a few hundred viewers. Its write routes
(`POST /events`, `/cues`, `/meta`) are guarded by `MATCHLAB_HUB_KEY` (header `X-Hub-Key`); reads are
open because the overlay is meant to be watched. Scaling to zero drops the in-memory state, which is
fine: a live match re-posts it and replays come from Cosmos DB. `hubAlwaysOn=true` keeps one replica
warm if the cold start bothers you. To go beyond one replica, put Web PubSub behind the same three
routes and Cosmos DB behind `/state`; nothing upstream changes.

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

## Two ways to deploy

- **From GitHub Actions** (`deploy` workflow): needs an identity GitHub can sign in as, with
  federated credentials. In a tenant where your user may not create app registrations, a
  user-assigned managed identity works the same way and needs only Contributor on the resource
  group to create:
  ```bash
  az identity create -n id-matchlab-deploy -g <rg> -o none
  az identity federated-credential create --identity-name id-matchlab-deploy -g <rg> --name github-main \
    --issuer https://token.actions.githubusercontent.com --subject repo:<owner>/matchlab:ref:refs/heads/main \
    --audiences api://AzureADTokenExchange
  PRINCIPAL=$(az identity show -n id-matchlab-deploy -g <rg> --query principalId -o tsv)
  az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal --role Contributor --scope $(az group show -n <rg> --query id -o tsv)
  az role assignment create --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal --role "User Access Administrator" --scope $(az group show -n <rg> --query id -o tsv)
  az identity show -n id-matchlab-deploy -g <rg> --query "{AZURE_CLIENT_ID:clientId, AZURE_TENANT_ID:tenantId}" -o yaml
  ```
  The two role assignments need Owner or User Access Administrator on the group; if they fail with
  `AuthorizationFailed`, use the second way.
- **From a laptop**: the `image` workflow builds and pushes the image on every push to `main`
  (no Azure identity involved), and `az deployment group create` with your own login does the rest.
  The template's own role assignments (Foundry, Cosmos DB) need the same rights; when the Foundry one
  is refused, leave `foundryAccountName` empty and pass the project's API key as `foundryApiKey`
  (`FOUNDRY_API_KEY`), which the fork job then uses instead of its identity.

## Deploy from GitHub Actions

1. **OIDC.** The identity above, with Contributor and User Access Administrator on the resource
   group; the template assigns the registry, Foundry and Cosmos DB roles itself, so it also needs
   Contributor on the groups holding the Foundry account and the Cosmos DB account when those differ.
2. **Repository secrets:** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
   `MATCHLAB_HUB_KEY` (any long random string), `EVENTHUB_CONNECTION_STRING` and `EVENTHUB_NAME`
   (from the Eventstream custom endpoint **destination** `matchlab-out`; the fork service reads the
   stream, it does not send to it), `KUSTO_QUERY_URI` and, to write fork records back,
   `KUSTO_INGEST_URI`.
3. **Repository variables:** `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`, `FOUNDRY_MODEL_ENDPOINT`
   (`https://<account>.services.ai.azure.com/openai/v1`), `FOUNDRY_DEPLOYMENT`,
   `FOUNDRY_ACCOUNT_NAME`, `COSMOS_ACCOUNT_NAME`; `FOUNDRY_RESOURCE_GROUP` and
   `COSMOS_RESOURCE_GROUP` only when they differ from the deployment group; `FORKD_SCHEDULE` (a cron
   in UTC) if the fork job should start itself.
4. **Image visibility.** The workflow pushes `ghcr.io/<owner>/matchlab`. Container Apps pulls it
   without credentials when the package is public: after the first run, open the package on GitHub
   (Packages, matchlab, Package settings) and set visibility to Public. To keep it private instead,
   add the secret `GHCR_PULL_TOKEN` (a token with `read:packages`) and the workflow passes it to the
   apps.
5. **Run the `deploy` workflow** (Actions, deploy, Run workflow). It builds the image, deploys, and
   waits for `/healthz` (which also wakes the hub once). The run summary prints the overlay URL, the
   job name and the identity's principal id.
6. **Give the identity access to Fabric.** In the Fabric workspace, Manage access, add `id-matchlab`
   as Contributor. The tenant setting "Service principals can use Fabric APIs" must be on for that
   workspace. Without this the Eventhouse source and `--ingest` fail with 401; the Event Hub source
   still works because it uses the connection string.

## Match day

The `matchday` workflow has five buttons: **kickoff** starts one fork-job execution (it follows the
next match on the stream and exits at full time), **status** lists executions and the hub's state,
**stop** ends a running execution, **hub-off** and **hub-on** stop and start the hub entirely (a
stopped hub does not wake on requests; with scale-to-zero you rarely need it). The same from a
terminal:

```bash
az containerapp job start -n matchlab-forkd -g <rg>                       # before kick-off
az containerapp job execution list -n matchlab-forkd -g <rg> -o table      # what ran, when
az containerapp logs show -n matchlab-hub -g <rg> --follow                # the hub's log
```

Then stream a match from anywhere:

```bash
matchlab stream data/matches/RAV-IRO-1000 --speed 10 --sink eventhub --batch 20 --live-ts
```

and open the overlay URL. Ghosts appear a few seconds after each fork-worthy decision, the words on
them are upgraded in place when the agent team's text verifies, and at full time the match is in the
replay picker.

## Deploy from a laptop

With the image on ghcr.io (pushed by the `image` workflow and made public once, or your own
`docker push`), `infra/main.bicepparam` filled in (`imageRef`, the account names) and the secrets in
the shell (`.env` already has `EVENTHUB_READ_CONNECTION_STRING`):

```bash
source .env
export HUB_KEY=$(openssl rand -hex 24)                                   # keep it: forkd on the laptop needs MATCHLAB_HUB_KEY=$HUB_KEY to post
az bicep build -f infra/main.bicep                                       # checks the template
az deployment group create -g rg-ai-garage-global -f infra/main.bicep -p infra/main.bicepparam
az deployment group show -g rg-ai-garage-global -n main --query properties.outputs -o yaml
```

Then `az containerapp job start -n matchlab-forkd -g rg-ai-garage-global` before a match and
stream from the laptop as usual. Re-running the same `az deployment group create` with a new
`imageRef` is the whole upgrade path.

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
