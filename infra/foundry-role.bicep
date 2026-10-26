// Gives the fork service's identity the Azure AI User role on the Foundry account, so
// DefaultAzureCredential can call the model deployment without keys.
targetScope = 'resourceGroup'

param foundryAccountName string
param principalId string

var azureAiUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: foundryAccountName
}

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, azureAiUser)
  scope: account
  properties: {
    roleDefinitionId: azureAiUser
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
