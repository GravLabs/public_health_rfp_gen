param envName string
param registryName string
param location string
param tags object = {}
param logAnalyticsCustomerId string      // workspace GUID (customerId property)
param logAnalyticsResourceId string      // full resource ID (for listKeys)

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false  // Use managed identity, not admin
  }
}

resource containerAppsEnv 'Microsoft.App/managedEnvironments@2023-11-02-preview' = {
  name: envName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: listKeys(logAnalyticsResourceId, '2022-10-01').primarySharedKey
      }
    }
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output environmentId string = containerAppsEnv.id
output environmentName string = containerAppsEnv.name
