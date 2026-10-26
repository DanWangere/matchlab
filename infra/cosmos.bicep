// The replay store on an existing Cosmos DB for NoSQL account (created in the portal with the free
// tier, or anywhere). Always: the data-plane role for the services' identity. Optionally
// (createContainer=true): database `matchlab` and container `matches` partitioned by /match_id at
// 400 RU/s, for an account where they were not made in Data Explorer.
targetScope = 'resourceGroup'

param cosmosAccountName string
param principalId string
param createContainer bool = false
param databaseName string = 'matchlab'
param containerName string = 'matches'
param throughput int = 400

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosAccountName
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = if (createContainer) {
  parent: account
  name: databaseName
  properties: {
    resource: { id: databaseName }
  }
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = if (createContainer) {
  parent: database
  name: containerName
  properties: {
    resource: {
      id: containerName
      partitionKey: { paths: ['/match_id'], kind: 'Hash', version: 2 }
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
