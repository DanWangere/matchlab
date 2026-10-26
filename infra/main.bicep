// matchlab on Azure Container Apps, sized to cost nothing while nothing is happening:
//   - the hub (overlay + SSE) scales to zero after five idle minutes and wakes on the first request;
//   - the fork service is a Container Apps *job*, started per match (by hand, a workflow button or a
//     cron), that follows one match with `--one-match` and exits at full time.
// Fabric (Eventstream, Eventhouse), the Foundry project and the Cosmos DB account are existing
// resources the services sign in to with one user-assigned identity.
//
// The image comes from a public registry by default (GitHub's ghcr.io, built by the deploy workflow),
// which has no standing cost; set createRegistry=true to build into an Azure Container Registry instead.

targetScope = 'resourceGroup'

@description('Azure region for everything in this file.')
param location string = resourceGroup().location

@description('Short name used as a prefix for resources; lowercase letters and digits.')
@minLength(3)
@maxLength(12)
param baseName string = 'matchlab'

@description('Full image reference to run, e.g. ghcr.io/<owner>/matchlab:abc123. Empty on the first phase when createRegistry is true.')
param imageRef string = ''

@description('Create an Azure Container Registry and pull from it with the identity (then imageRef is <registry>/<baseName>:<imageTag>).')
param createRegistry bool = false

@description('Image tag in the created registry (createRegistry only).')
param imageTag string = ''

@description('Username for a private image registry (empty for a public image).')
param registryUsername string = ''

@description('Password or token for that registry.')
@secure()
param registryPassword string = ''

@description('Write key for the hub (POST /events, /cues, /meta). Readers need none. Container Apps rejects an empty secret, so it is required.')
@secure()
param hubKey string

@description('Where the fork service reads the match from: the Eventstream custom endpoint destination (live) or the Eventhouse table (replay / follow).')
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

@description('Cron (5 fields, UTC) to start the fork job on a schedule, e.g. "0 14 * * 6"; empty means manual start only.')
param forkdSchedule string = ''

@description('Longest a fork job may run, in seconds (a real-time match plus stoppage is about 7000).')
param forkdTimeoutSeconds int = 9000

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

@description('Name of an existing Cosmos DB for NoSQL account for the replay store; empty leaves replay off.')
param cosmosAccountName string = ''

@description('Resource group of that account (defaults to this one).')
param cosmosResourceGroup string = resourceGroup().name

@description('Rollouts per fork; 120 is about 0.5 s per fork on one vCPU.')
param rollouts int = 120

@description('Keep one hub replica warm (minReplicas 1) instead of scaling to zero. Costs about $0.05 an hour.')
param hubAlwaysOn bool = false

var registryName = toLower(replace('${baseName}acr${uniqueString(resourceGroup().id)}', '-', ''))
// deployApps must be decidable before deployment starts (it gates resources), so it looks only at parameters.
var deployApps = createRegistry ? !empty(imageTag) : !empty(imageRef)
var image = createRegistry ? (empty(imageTag) ? '' : '${registry!.properties.loginServer}/${baseName}:${imageTag}') : imageRef
var acrPullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var registryServer = createRegistry ? registry!.properties.loginServer : (empty(imageRef) ? '' : split(imageRef, '/')[0])
var privateRegistry = !createRegistry && !empty(registryPassword)

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

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = if (createRegistry) {
  name: registryName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRegistry) {
  name: guid(resourceGroup().id, registryName, identity.id, acrPullRole)
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

var cosmosEndpoint = empty(cosmosAccountName) ? '' : cosmos!.outputs.endpoint
var cosmosEnv = [
  { name: 'COSMOS_ENDPOINT', value: cosmosEndpoint }
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
]

// How the apps pull the image: the identity for a created registry, a username/password for a
// private public-cloud registry, nothing for a public image.
var registries = createRegistry ? [
  { server: registryServer, identity: identity.id }
] : privateRegistry ? [
  { server: registryServer, username: registryUsername, passwordSecretRef: 'registry-password' }
] : []
var registrySecrets = privateRegistry ? [{ name: 'registry-password', value: registryPassword }] : []

// The hub keeps the current match in memory and fans it out over SSE. At zero replicas it costs
// nothing; the first request (a viewer, or the fork job posting an event) wakes it in a few seconds.
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
      registries: registries
      secrets: concat([{ name: 'hub-key', value: hubKey }], registrySecrets)
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
      scale: {
        minReplicas: hubAlwaysOn ? 1 : 0
        maxReplicas: 1
        rules: [
          { name: 'http', http: { metadata: { concurrentRequests: '200' } } }
        ]
      }
    }
  }
  dependsOn: [acrPull]
}

// Bicep arrays are newline-separated: no commas at line ends inside the brackets.
var forkdBaseArgs = [
  'matchlab'
  'forkd'
  '--source'
  forkdSource
  '--hub'
  'http://${baseName}-hub'
  '--mirror'
  '--agents'
  agents
  '--rollouts'
  string(rollouts)
  '--out'
  '/tmp/runs/forks.jsonl'
  '--quiet'
  '--one-match'
]
var forkdArgs = concat(
  forkdBaseArgs,
  forkdSource == 'eventhub' ? ['--consumer-group', eventHubConsumerGroup] : ['--match', forkdMatch, '--follow'],
  empty(kustoIngestUri) ? [] : ['--ingest']
)

// Container Apps rejects empty secret values, so the Event Hub secret exists only when a string was given.
var forkdSecrets = concat(
  [{ name: 'hub-key', value: hubKey }],
  empty(eventHubConnectionString) ? [] : [{ name: 'eventhub-cs', value: eventHubConnectionString }],
  registrySecrets
)
var forkdEnv = concat(
  [{ name: 'MATCHLAB_HUB_KEY', secretRef: 'hub-key' }],
  empty(eventHubConnectionString) ? [] : [{ name: 'EVENTHUB_CONNECTION_STRING', secretRef: 'eventhub-cs' }]
)

// The fork service runs as a job: one execution per match, exiting at full time, billed only while it runs.
resource forkd 'Microsoft.App/jobs@2024-03-01' = if (deployApps) {
  name: '${baseName}-forkd'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: env.id
    configuration: {
      triggerType: empty(forkdSchedule) ? 'Manual' : 'Schedule'
      replicaTimeout: forkdTimeoutSeconds
      replicaRetryLimit: 0
      manualTriggerConfig: empty(forkdSchedule) ? { parallelism: 1, replicaCompletionCount: 1 } : null
      scheduleTriggerConfig: empty(forkdSchedule) ? null : { cronExpression: forkdSchedule, parallelism: 1, replicaCompletionCount: 1 }
      registries: registries
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
    }
  }
  dependsOn: [acrPull, hub]
}

output registryLoginServer string = createRegistry ? registry!.properties.loginServer : ''
output identityPrincipalId string = identity.properties.principalId
output identityClientId string = identity.properties.clientId
output hubUrl string = deployApps ? 'https://${hub!.properties.configuration.ingress.fqdn}' : ''
output forkJobName string = deployApps ? '${baseName}-forkd' : ''
output cosmosEndpoint string = cosmosEndpoint
