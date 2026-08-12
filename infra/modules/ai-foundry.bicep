param hubName string
param projectName string
param location string
param tags object = {}
param identityPrincipalId string
param openAiResourceId string
param searchResourceId string
param storageResourceId string
param keyVaultResourceId string
param appInsightsResourceId string

// AI Foundry Hub
resource hub 'Microsoft.MachineLearningServices/workspaces@2024-04-01' = {
  name: hubName
  location: location
  tags: tags
  kind: 'Hub'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    storageAccount: storageResourceId
    keyVault: keyVaultResourceId
    applicationInsights: appInsightsResourceId
    publicNetworkAccess: 'Enabled'
  }
}

// AI Foundry Project (scoped to the Hub)
resource project 'Microsoft.MachineLearningServices/workspaces@2024-04-01' = {
  name: projectName
  location: location
  tags: tags
  kind: 'Project'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    hubResourceId: hub.id
    publicNetworkAccess: 'Enabled'
  }
}

// Connect Azure OpenAI to the Hub
resource openAiConnection 'Microsoft.MachineLearningServices/workspaces/connections@2024-04-01' = {
  parent: hub
  name: 'azure-openai'
  properties: {
    category: 'AzureOpenAI'
    target: 'https://${split(openAiResourceId, '/')[8]}.openai.azure.com'
    authType: 'ManagedIdentity'
    metadata: {
      ResourceId: openAiResourceId
    }
  }
}

// Connect AI Search to the Hub
resource searchConnection 'Microsoft.MachineLearningServices/workspaces/connections@2024-04-01' = {
  parent: hub
  name: 'azure-ai-search'
  properties: {
    category: 'CognitiveSearch'
    target: 'https://${split(searchResourceId, '/')[8]}.search.windows.net'
    authType: 'ManagedIdentity'
    metadata: {
      ResourceId: searchResourceId
    }
  }
}

// Grant managed identity AzureML Data Scientist on the Hub
resource mlDataScientist 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(hub.id, identityPrincipalId, 'f6c7c914-8db3-469d-8ca1-694a8f32e121')
  scope: hub
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'f6c7c914-8db3-469d-8ca1-694a8f32e121')
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output hubId string = hub.id
output projectId string = project.id
output projectEndpoint string = 'https://${location}.api.azureml.ms/rp/workspaces/subscriptions/${subscription().subscriptionId}/resourceGroups/${resourceGroup().name}/providers/Microsoft.MachineLearningServices/workspaces/${projectName}'
