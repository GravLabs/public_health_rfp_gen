param name string
param location string
param tags object = {}
param skuName string = 'basic'
param identityPrincipalId string

resource search 'Microsoft.Search/searchServices@2024-03-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName  // basic ~$75/mo vs S1 ~$250/mo
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
    semanticSearch: 'standard'  // Enable semantic ranker
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http403'
      }
    }
  }
  identity: {
    type: 'SystemAssigned'
  }
}

// Grant managed identity Search Index Data Contributor
resource searchDataContrib 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, identityPrincipalId, '8ebe5a00-799e-43f5-93ac-243d3dce84a7')
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8ebe5a00-799e-43f5-93ac-243d3dce84a7')
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Grant managed identity Search Service Contributor
resource searchServiceContrib 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, identityPrincipalId, '7ca78c08-252a-4471-8644-bb5ff32d4ba0')
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7ca78c08-252a-4471-8644-bb5ff32d4ba0')
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output endpoint string = 'https://${search.name}.search.windows.net'
output resourceId string = search.id
output name string = search.name
