// Laptop deploy:  az deployment group create -g rg-ai-garage-global -f infra/main.bicep -p infra/main.bicepparam
// The deploy workflow passes the same values from GitHub secrets and variables instead.
using 'main.bicep'

param baseName = 'matchlab'
param imageRef = 'ghcr.io/<owner>/matchlab:latest'   // a public image; or createRegistry=true + imageTag for an Azure registry
param createRegistry = false
param hubAlwaysOn = false                // true keeps one replica warm, about $0.05 an hour
param forkdSource = 'eventhub'
param eventHubName = ''                  // from the Eventstream custom endpoint destination matchlab-out
param eventHubConsumerGroup = '$Default'
param forkdSchedule = ''                 // cron in UTC to start the fork job automatically, e.g. '0 14 * * 6'; empty = manual
param kustoQueryUri = ''                 // https://<host>.kusto.fabric.microsoft.com
param kustoIngestUri = ''                // https://ingest-<host>.kusto.fabric.microsoft.com, or leave empty
param foundryModelEndpoint = 'https://aif-garage-swedencentral.services.ai.azure.com/openai/v1'
param foundryDeployment = 'gpt-5.6-sol'
param foundryAccountName = 'aif-garage-swedencentral'
param foundryResourceGroup = 'rg-ai-garage-global'
param cosmosAccountName = 'cosmos-matchlab'
param cosmosResourceGroup = 'rg-ai-garage-global'
param agents = 'foundry'
// Secrets come from the environment:  export HUB_KEY=... EVENTHUB_CONNECTION_STRING=...  (the destination's string)
param hubKey = readEnvironmentVariable('HUB_KEY', '')
param eventHubConnectionString = readEnvironmentVariable('EVENTHUB_READ_CONNECTION_STRING', '')
