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

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: embeddingModelName
  dependsOn: [gptDeployment]
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

// GPT-4o-mini — evaluation gate (groundedness, completeness, coherence) and classification
// ~15x cheaper than GPT-4o; sufficient for structured scoring tasks
resource miniDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: 'gpt-4o-mini'
  dependsOn: [embeddingDeployment]
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

// o3-mini — parameter accuracy evaluation and budget audit
// Reasoning model; catches arithmetic and parameter-matching edge cases GPT-4o misses
resource o3MiniDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: 'o3-mini'
  dependsOn: [miniDeployment]
  sku: {
    name: 'GlobalStandard'
    capacity: 10
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'o3-mini'
      version: '2025-01-31'
    }
  }
}

// Grant managed identity Cognitive Services OpenAI User
resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAi.id, identityPrincipalId, '5e0bd9bd-7b93-4f28-af87-19fc36ad1654')
  scope: openAi
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad1654')
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output endpoint string = openAi.properties.endpoint
output resourceId string = openAi.id
output name string = openAi.name
