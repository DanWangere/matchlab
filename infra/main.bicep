// matchlab on Azure Container Apps: the cue hub (renderer + SSE) and the Otherwise fork service,
// one image, one user-assigned identity. Fabric (Eventstream, Eventhouse) and the Foundry project
// are not created here; they are existing resources the fork service signs in to with that identity.
//
// Two-phase deploy (see .github/workflows/deploy.yml): first without imageTag, which creates the
// registry, identity and environment; then build and push the image; then again with imageTag,
// which creates the two apps.

targetScope = 'resourceGroup'

@description('Azure region for everything in this file.')
param location string = resourceGroup().location

@description('Short name used as a prefix for resources; lowercase letters and digits.')
@minLength(3)
@maxLength(12)
param baseName string = 'matchlab'

@description('Image tag in the registry; empty on the first phase (no apps yet).')
param imageTag string = ''

@description('Write key for the hub (POST /events, /cues, /meta). Readers need none. Required: Container Apps rejects an empty secret.')
@secure()
param hubKey string

@description('Where the fork service reads the match from: the Eventstream custom endpoint (live) or the Eventhouse table (replay / follow).')
@allowed(['eventhub', 'eventhouse'])
param forkdSource string = 'eventhub'

@description('Connection string of the Eventstream custom endpoint destination the fork service reads (SAS Key Authentication).')
@secure()
param eventHubConnectionString string = ''

@description('Event Hub name shown on that destination.')
param eventHubName string = ''

@description('Consumer group on that destination; $Default unless you added one.')
param eventHubConsumerGroup string = '$Default'

@description('Eventhouse query URI (the KQL database\'s Query URI).')
param kustoQueryUri string = ''

@description('Eventhouse ingestion URI (the Query URI with "ingest-" in front of the host). Empty disables --ingest.')
param kustoIngestUri string = ''

@description('KQL database name.')
param kustoDatabase string = 'matchlab'

@description('Match id to replay when forkdSource is eventhouse.')
param forkdMatch string = ''

@description('Agent team: foundry (gpt model through the Foundry project), framework (same, via Microsoft Agent Framework), fake, or off.')
@allowed(['foundry', 'framework', 'fake', 'off'])
param agents string = 'foundry'

@description('Foundry model endpoint, e.g. https://<account>.services.ai.azure.com/openai/v1')
param foundryModelEndpoint string = ''

@description('Foundry model deployment name.')
param foundryDeployment string = 'gpt-5.6-sol'

@description('Name of the Foundry (AI Services) account the identity needs the Azure AI User role on; empty skips the role assignment.')
param foundryAccountName string = ''

@description('Resource group of that Foundry account (defaults to this one).')
param foundryResourceGroup string = resourceGroup().name

@description('Rollouts per fork; 120 is about 0.5 s per fork on one vCPU.')
param rollouts int = 120

@description('Name of an existing Cosmos DB for NoSQL account for the replay store; empty leaves replay off.')
param cosmosAccountName string = ''

@description('Resource group of that account (defaults to this one).')
param cosmosResourceGroup string = resourceGroup().name

var registryName = toLower(replace('${baseName}acr${uniqueString(resourceGroup().id)}', '-', ''))
var deployApps = !empty(imageTag)
var image = '${registry.properties.loginServer}/${baseName}:${imageTag}'
var acrPullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${baseName}'
  location: location
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${baseName}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: registryName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRole)
  scope: registry
  properties: {
    roleDefinitionId: acrPullRole
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${baseName}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

module foundryRole 'foundry-role.bicep' = if (!empty(foundryAccountName)) {
  name: 'foundry-role-${baseName}'
  scope: resourceGroup(foundryResourceGroup)
  params: {
    foundryAccountName: foundryAccountName
    principalId: identity.properties.principalId
  }
}

module cosmos 'cosmos.bicep' = if (!empty(cosmosAccountName)) {
  name: 'cosmos-${baseName}'
  scope: resourceGroup(cosmosResourceGroup)
  params: {
    cosmosAccountName: cosmosAccountName
    principalId: identity.properties.principalId
  }
}

var cosmosEndpoint = empty(cosmosAccountName) ? '' : cosmos.outputs.endpoint
var cosmosEnv = [
  { name: 'COSMOS_ENDPOINT', value: cosmosEndpoint }
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
]

// The hub keeps cues and recent events in memory and fans them out over SSE, so it runs as one
// replica. Swap in Web PubSub + Cosmos DB behind the same three POST routes to scale out.
resource hub 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${baseName}-hub'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
      registries: [
        { server: registry.properties.loginServer, identity: identity.id }
      ]
      secrets: [
        { name: 'hub-key', value: hubKey }
      ]
    }
    template: {
      containers: [
        {
          name: 'hub'
          image: image
          command: ['matchlab', 'serve', '--port', '8080']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat([
            { name: 'MATCHLAB_HUB_KEY', secretRef: 'hub-key' }
          ], cosmosEnv)
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 10 }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [acrPull]
}

var forkdArgs = concat(
  ['matchlab', 'forkd', '--source', forkdSource, '--hub', 'http://${baseName}-hub', '--mirror', '--agents', agents,
   '--rollouts', string(rollouts), '--out', '/tmp/runs/forks.jsonl', '--quiet'],
  forkdSource == 'eventhub' ? ['--consumer-group', eventHubConsumerGroup] : ['--match', forkdMatch, '--follow'],
  empty(kustoIngestUri) ? [] : ['--ingest']
)

// Container Apps rejects empty secret values, so the Event Hub secret exists only when a string was given.
var forkdSecrets = concat(
  [{ name: 'hub-key', value: hubKey }],
  empty(eventHubConnectionString) ? [] : [{ name: 'eventhub-cs', value: eventHubConnectionString }]
)
var forkdEnv = concat(
  [{ name: 'MATCHLAB_HUB_KEY', secretRef: 'hub-key' }],
  empty(eventHubConnectionString) ? [] : [{ name: 'EVENTHUB_CONNECTION_STRING', secretRef: 'eventhub-cs' }]
)

resource forkd 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: '${baseName}-forkd'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      registries: [
        { server: registry.properties.loginServer, identity: identity.id }
      ]
      secrets: forkdSecrets
    }
    template: {
      containers: [
        {
          name: 'forkd'
          image: image
          command: forkdArgs
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: concat(forkdEnv, [
            { name: 'EVENTHUB_NAME', value: eventHubName }
            { name: 'KUSTO_QUERY_URI', value: kustoQueryUri }
            { name: 'KUSTO_INGEST_URI', value: kustoIngestUri }
            { name: 'KUSTO_DATABASE', value: kustoDatabase }
            { name: 'FOUNDRY_MODEL_ENDPOINT', value: foundryModelEndpoint }
            { name: 'FOUNDRY_DEPLOYMENT', value: foundryDeployment }
          ], cosmosEnv)
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [acrPull, hub]
}

output registryLoginServer string = registry.properties.loginServer
output identityPrincipalId string = identity.properties.principalId
output identityClientId string = identity.properties.clientId
output hubUrl string = deployApps ? 'https://${hub.properties.configuration.ingress.fqdn}' : ''
output cosmosEndpoint string = cosmosEndpoint
