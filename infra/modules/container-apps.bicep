param envName string
param registryName string
param location string
param tags object = {}
param logAnalyticsCustomerId string      // workspace GUID (customerId property)
param logAnalyticsResourceId string      // full resource ID (for listKeys)
param identityId string                  // user-assigned managed identity resource ID
param identityPrincipalId string         // object/principal ID (for RBAC role assignment)
param apiAppName string
param orchestratorAppName string
param apiEnvVars array = []
param orchestratorEnvVars array = []

var placeholder = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

// AcrPull built-in role
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identityId, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
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

// Orchestrator defined first so its FQDN can be referenced by apiApp
resource orchestratorApp 'Microsoft.App/containerApps@2023-11-02-preview' = {
  name: orchestratorAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'orchestrator' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      ingress: {
        external: false
        targetPort: 5001
        transport: 'http'
      }
      registries: [{ server: registry.properties.loginServer, identity: identityId }]
    }
    template: {
      containers: [{
        name: 'orchestrator'
        image: placeholder
        resources: { cpu: json('1'), memory: '2Gi' }
        env: orchestratorEnvVars
      }]
      scale: { minReplicas: 1, maxReplicas: 2 }
    }
  }
}

resource apiApp 'Microsoft.App/containerApps@2023-11-02-preview' = {
  name: apiAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
      }
      registries: [{ server: registry.properties.loginServer, identity: identityId }]
    }
    template: {
      containers: [{
        name: 'api'
        image: placeholder
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: union(apiEnvVars, [{ name: 'ORCHESTRATOR_URL', value: 'https://${orchestratorApp.properties.configuration.ingress.fqdn}' }])
      }]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output environmentId string = containerAppsEnv.id
output environmentName string = containerAppsEnv.name
output apiAppName string = apiApp.name
output apiAppFqdn string = apiApp.properties.configuration.ingress.fqdn
output orchestratorAppName string = orchestratorApp.name
