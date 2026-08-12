param name string
param location string
param tags object = {}
param identityPrincipalId string

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    isHnsEnabled: true  // ADLS Gen2
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    networkAcls: {
      defaultAction: 'Allow'  // Restrict to VNet in production
    }
  }
}

resource rfpCorpusContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  name: '${storage.name}/default/rfp-corpus'
  properties: {
    publicAccess: 'None'
  }
}

resource goldenDatasetContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  name: '${storage.name}/default/golden-dataset'
  properties: {
    publicAccess: 'None'
  }
}

// Grant managed identity Storage Blob Data Contributor
resource blobContrib 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identityPrincipalId, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output name string = storage.name
output resourceId string = storage.id
output primaryEndpoint string = storage.properties.primaryEndpoints.blob
