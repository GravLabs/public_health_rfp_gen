param name string
param location string
param tags object = {}
param identityPrincipalId string
param gptModelName string = 'gpt-4o'
param gptModelVersion string = '2024-11-20'
param embeddingModelName string = 'text-embedding-3-small'

resource openAi 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: name
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
  }
  identity: {
    type: 'SystemAssigned'
  }
}

resource gptDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: gptModelName
  sku: {
    name: 'GlobalStandard'
    capacity: 30  // 30K TPM — sufficient for POC within budget
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: gptModelName
      version: gptModelVersion
    }
  }
}

resource miniDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: 'gpt-4o-mini'
  dependsOn: [gptDeployment]
  sku: {
    name: 'GlobalStandard'
    capacity: 30
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4o-mini'
      version: '2024-07-18'
    }
  }
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: embeddingModelName
  dependsOn: [miniDeployment]
  sku: {
    name: 'Standard'
    capacity: 30
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: '1'
    }
  }
}

// Grant managed identity Cognitive Services OpenAI User
resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAi.id, identityPrincipalId, 'a97b65f3-24c7-4388-baec-2e87135dc908')
  scope: openAi
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output endpoint string = openAi.properties.endpoint
output resourceId string = openAi.id
output name string = openAi.name
