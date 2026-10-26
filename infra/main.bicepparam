// Fill in and run:  az deployment group create -g rg-matchlab -f infra/main.bicep -p infra/main.bicepparam
// The deploy workflow passes the same values from GitHub secrets and variables instead.
using 'main.bicep'

param baseName = 'matchlab'
param imageTag = ''                      // set after `az acr build` (the workflow does both phases)
param forkdSource = 'eventhub'
param eventHubName = ''                  // from the Eventstream custom endpoint source
param eventHubConsumerGroup = 'forkd'
param kustoQueryUri = ''                 // https://<host>.kusto.fabric.microsoft.com
param kustoIngestUri = ''                // https://ingest-<host>.kusto.fabric.microsoft.com, or leave empty
param foundryModelEndpoint = 'https://<account>.services.ai.azure.com/openai/v1'
param foundryDeployment = 'gpt-5.6-sol'
param foundryAccountName = ''            // the AI Services account; empty skips the role assignment
param foundryResourceGroup = 'rg-matchlab'
param agents = 'foundry'
// Secrets come from the environment:  export HUB_KEY=... EVENTHUB_CONNECTION_STRING=...
param hubKey = readEnvironmentVariable('HUB_KEY', '')
param eventHubConnectionString = readEnvironmentVariable('EVENTHUB_CONNECTION_STRING', '')
