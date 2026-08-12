param name string
param location string
param tags object = {}
param identityPrincipalId string

resource docIntelligence 'Microsoft.CognitiveServices/accounts@2023-10-01-preview' = {
  name: name
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: {
    name: 'S0'  // Standard S0: 1500 pages/mo free, then $1.50/1000 pages
  }
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
  }
}

// Grant managed identity Cognitive Services User
resource cogUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(docIntelligence.id, identityPrincipalId, 'a97b65f3-24c7-4388-baec-2e87135dc908')
  scope: docIntelligence
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output endpoint string = docIntelligence.properties.endpoint
output resourceId string = docIntelligence.id
output name string = docIntelligence.name
