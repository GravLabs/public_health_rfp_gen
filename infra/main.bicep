targetScope = 'subscription'

@description('Environment name (dev, staging, prod)')
param environmentName string

@description('Azure region for all resources')
param location string = 'eastus'

param apimPublisherEmail string = 'admin@example.com'
param ownerEmail string = 'owner@example.com'
param openAiModelName string = 'gpt-4o'
param openAiModelVersion string = '2024-08-06'
param embeddingModelName string = 'text-embedding-3-small'
param searchSkuName string = 'basic'
param budgetAmountUsd int = 500
@description('Client ID of the Azure AD App Registration for the Teams bot — must exist before deploy (msaAppType is SingleTenant, validated at provision time)')
param botAppId string
@secure()
param botAppSecret string = ''
param tags object = {}

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var prefix = '${abbrs.resourcesResourceGroups}${environmentName}'

// Resource Group
resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: '${prefix}-${resourceToken}'
  location: location
  tags: tags
}

// User-assigned managed identity (used by all services — no keys in code)
module identity 'modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    name: '${abbrs.managedIdentityUserAssignedIdentities}pubhealth-${resourceToken}'
    location: location
    tags: tags
  }
}

// Storage Account (ADLS Gen2 for raw RFP corpus)
module storage 'modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    name: '${abbrs.storageStorageAccounts}pubhealth${resourceToken}'
    location: location
    tags: tags
    identityPrincipalId: identity.outputs.principalId
  }
}

// Key Vault
module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  scope: rg
  params: {
    name: 'kvph${resourceToken}'
    location: location
    tags: tags
    identityPrincipalId: identity.outputs.principalId
  }
}

// Azure OpenAI
module openAi 'modules/openai.bicep' = {
  name: 'openai'
  scope: rg
  params: {
    name: '${abbrs.cognitiveServicesAccounts}pubhealth-oai-${resourceToken}'
    location: location
    tags: tags
    identityPrincipalId: identity.outputs.principalId
    gptModelName: openAiModelName
    gptModelVersion: openAiModelVersion
    embeddingModelName: embeddingModelName
  }
}

// Azure AI Document Intelligence
module docIntelligence 'modules/doc-intelligence.bicep' = {
  name: 'docIntelligence'
  scope: rg
  params: {
    name: '${abbrs.cognitiveServicesAccounts}pubhealth-di-${resourceToken}'
    location: location
    tags: tags
    identityPrincipalId: identity.outputs.principalId
  }
}

// Azure AI Search
module search 'modules/ai-search.bicep' = {
  name: 'search'
  scope: rg
  params: {
    name: '${abbrs.searchSearchServices}pubhealth-${resourceToken}'
    location: location
    tags: tags
    skuName: searchSkuName
    identityPrincipalId: identity.outputs.principalId
  }
}

// Log Analytics + Application Insights
module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    logAnalyticsName: '${abbrs.operationalInsightsWorkspaces}pubhealth-${resourceToken}'
    appInsightsName: '${abbrs.insightsComponents}pubhealth-${resourceToken}'
    location: location
    tags: tags
  }
}

// Azure AI Foundry Hub + Project
module aiFoundry 'modules/ai-foundry.bicep' = {
  name: 'aiFoundry'
  scope: rg
  params: {
    hubName: '${abbrs.machineLearningWorkspaces}pubhealth-hub-${resourceToken}'
    projectName: '${abbrs.machineLearningWorkspaces}pubhealth-rfp-${resourceToken}'
    location: location
    tags: tags
    identityPrincipalId: identity.outputs.principalId
    mlStorageName: 'stml${resourceToken}'
    keyVaultResourceId: keyVault.outputs.resourceId
    appInsightsResourceId: monitoring.outputs.appInsightsId
  }
}

// Container Apps Environment + Registry + Container Apps
module containerApps 'modules/container-apps.bicep' = {
  name: 'containerApps'
  scope: rg
  params: {
    envName: '${abbrs.appManagedEnvironments}pubhealth-${resourceToken}'
    registryName: '${abbrs.containerRegistryRegistries}pubhealth${resourceToken}'
    location: location
    tags: tags
    logAnalyticsCustomerId: monitoring.outputs.logAnalyticsWorkspaceId
    logAnalyticsResourceId: monitoring.outputs.logAnalyticsId
    identityId: identity.outputs.identityId
    identityPrincipalId: identity.outputs.principalId
    apiAppName: 'ca-pubhealth-api-${resourceToken}'
    orchestratorAppName: 'ca-pubhealth-orch-${resourceToken}'
    orchestratorEnvVars: [
      { name: 'AZURE_CLIENT_ID', value: identity.outputs.clientId }
      { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.outputs.endpoint }
      { name: 'AZURE_OPENAI_GPT_DEPLOYMENT', value: openAiModelName }
      { name: 'AZURE_OPENAI_MINI_DEPLOYMENT', value: 'gpt-4o-mini' }
      { name: 'AZURE_SEARCH_ENDPOINT', value: search.outputs.endpoint }
      { name: 'AZURE_SEARCH_INDEX', value: 'pubhealth-rfp-index' }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: monitoring.outputs.appInsightsConnectionString }
    ]
    apiEnvVars: [
      { name: 'AZURE_CLIENT_ID', value: identity.outputs.clientId }
      { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.outputs.endpoint }
      { name: 'AZURE_OPENAI_GPT_DEPLOYMENT', value: openAiModelName }
      { name: 'AZURE_OPENAI_MINI_DEPLOYMENT', value: 'gpt-4o-mini' }
      { name: 'AZURE_OPENAI_O3_DEPLOYMENT', value: 'gpt-4o' }
      { name: 'AZURE_OPENAI_EMBEDDING_DEPLOYMENT', value: embeddingModelName }
      { name: 'AZURE_SEARCH_ENDPOINT', value: search.outputs.endpoint }
      { name: 'AZURE_SEARCH_INDEX', value: 'pubhealth-rfp-index' }
      { name: 'AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT', value: docIntelligence.outputs.endpoint }
      { name: 'AZURE_STORAGE_ACCOUNT', value: storage.outputs.name }
      { name: 'AZURE_STORAGE_CONTAINER', value: 'rfp-corpus' }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: monitoring.outputs.appInsightsConnectionString }
      { name: 'AZURE_APIM_GATEWAY_URL', value: apim.outputs.gatewayUrl }
      { name: 'AZURE_AI_FOUNDRY_PROJECT_ENDPOINT', value: aiFoundry.outputs.projectEndpoint }
      { name: 'MICROSOFT_APP_ID', value: botAppId }
      { name: 'MICROSOFT_APP_PASSWORD', value: botAppSecret }
      { name: 'MICROSOFT_APP_TYPE', value: 'SingleTenant' }
      { name: 'MICROSOFT_APP_TENANT_ID', value: tenant().tenantId }
      { name: 'MONTHLY_BUDGET_USD', value: '500' }
      { name: 'BUDGET_WARN_THRESHOLD', value: '0.80' }
      { name: 'BUDGET_CRITICAL_THRESHOLD', value: '0.95' }
      { name: 'GPT4O_PROMPT_COST_PER_1K', value: '0.0025' }
      { name: 'GPT4O_COMPLETION_COST_PER_1K', value: '0.010' }
    ]
  }
}

// AI Gateway — APIM with semantic caching, token budgets, backend pool
module apim 'modules/apim.bicep' = {
  name: 'apim'
  scope: rg
  params: {
    name: '${abbrs.apiManagementService}pubhealth-${resourceToken}'
    location: location
    tags: tags
    publisherEmail: apimPublisherEmail
    openAiEndpoint: openAi.outputs.endpoint
    openAiResourceId: openAi.outputs.resourceId
    appInsightsId: monitoring.outputs.appInsightsId
    appInsightsInstrumentationKey: monitoring.outputs.instrumentationKey
  }
}

module botService 'modules/bot-service.bicep' = {
  name: 'botService'
  scope: rg
  params: {
    name: 'bot-pubhealth-rfp-${resourceToken}'
    tags: tags
    messagingEndpoint: 'https://${containerApps.outputs.apiAppFqdn}/api/messages'
    microsoftAppId: botAppId
    tenantId: tenant().tenantId
  }
}

// Azure Budget Alert ($500/mo)
module budget 'modules/budget.bicep' = {
  name: 'budget'
  scope: rg
  params: {
    budgetName: 'pubhealth-rfp-poc-budget'
    amount: budgetAmountUsd
    contactEmails: [ownerEmail]
  }
}

// Outputs used by AZD and application code
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_CLIENT_ID string = identity.outputs.clientId

output AZURE_OPENAI_ENDPOINT string = openAi.outputs.endpoint
output AZURE_OPENAI_GPT_DEPLOYMENT string = openAiModelName
output AZURE_OPENAI_EMBEDDING_DEPLOYMENT string = embeddingModelName

output AZURE_SEARCH_ENDPOINT string = search.outputs.endpoint
output AZURE_SEARCH_INDEX string = 'pubhealth-rfp-index'

output AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT string = docIntelligence.outputs.endpoint

output AZURE_STORAGE_ACCOUNT string = storage.outputs.name
output AZURE_STORAGE_CONTAINER string = 'rfp-corpus'

output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString

output AZURE_APIM_GATEWAY_URL string = apim.outputs.gatewayUrl
output AZURE_APIM_NAME string = apim.outputs.apimName

output AZURE_AI_FOUNDRY_PROJECT_ENDPOINT string = aiFoundry.outputs.projectEndpoint
output AZURE_AI_FOUNDRY_HUB_NAME string = '${abbrs.machineLearningWorkspaces}pubhealth-hub-${resourceToken}'
output AZURE_AI_FOUNDRY_PROJECT_NAME string = '${abbrs.machineLearningWorkspaces}pubhealth-rfp-${resourceToken}'

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerApps.outputs.registryLoginServer

output AZURE_BOT_NAME string = botService.outputs.botName
