param hubName string
param projectName string
param location string
param tags object = {}
param identityPrincipalId string
param mlStorageName string      // separate non-HNS storage (AI Foundry doesn't support ADLS Gen2)
param keyVaultResourceId string
param appInsightsResourceId string

// Standard storage for ML workspace — HNS disabled (AI Foundry rejects ADLS Gen2)
resource mlStorage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: mlStorageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    isHnsEnabled: false
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

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
    storageAccount: mlStorage.id
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
