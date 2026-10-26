// The replay store on an existing Cosmos DB for NoSQL account (created in the portal with the free
// tier, or anywhere): database `matchlab`, container `matches` partitioned by /match_id at 400 RU/s
// (inside the free tier's 1000), and the data-plane role for the services' identity.
targetScope = 'resourceGroup'

param cosmosAccountName string
param principalId string
param databaseName string = 'matchlab'
param containerName string = 'matches'
param throughput int = 400

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosAccountName
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: { id: databaseName }
  }
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: containerName
  properties: {
    resource: {
      id: containerName
      partitionKey: { paths: ['/match_id'], kind: 'Hash' }
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [{ path: '/match_id/?' }, { path: '/type/?' }, { path: '/id/?' }, { path: '/meta/kickoff/?' }]
        excludedPaths: [{ path: '/*' }]   // event rows and cue text are read whole, never filtered on
      }
    }
    options: { throughput: throughput }
  }
}

// Cosmos DB Built-in Data Contributor
resource dataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: account
  name: guid(account.id, principalId, 'data-contributor')
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    principalId: principalId
    scope: account.id
  }
}

output endpoint string = account.properties.documentEndpoint
