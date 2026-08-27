// Unified Azure AI Foundry resource — replaces the old openai.bicep (kind:
// 'OpenAI') + ai-foundry.bicep (legacy Microsoft.MachineLearningServices Hub +
// Project) split. A single Microsoft.CognitiveServices/accounts resource with
// kind 'AIServices' and allowProjectManagement:true serves the exact same
// classic OpenAI REST routes (/openai/deployments/{name}/chat/completions)
// as the old 'OpenAI'-kind account -- confirmed via Azure's own OpenAI API
// backward-compatibility guarantee -- while also exposing a real Foundry
// Project (Studio, connections, eval-run tracking) and native Content Safety
// (/contentsafety/*) on the same resource, with no separate ML Workspace Hub
// and no dedicated non-HNS storage account needed.
param name string
param location string
param tags object = {}
param identityPrincipalId string
param gptModelName string = 'gpt-4o'
param gptModelVersion string = '2024-11-20'
param embeddingModelName string = 'text-embedding-3-small'
param searchEndpoint string = ''

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: name
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true
  }
  identity: {
    type: 'SystemAssigned'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: account
  name: '${name}-project'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Public Health RFP'
    description: 'Public Health Laboratory RFP generation POC'
  }
}

// AI Search connection — genuinely external to this account (unlike the OpenAI
// deployments below, which live natively on this account and need no
// connection object to be usable from the project). AAD auth, no keys, same
// as everywhere else in this project. Lets Foundry Studio's playground/prompt
// flow and any future agent tooling reference the corpus index directly.
resource searchConnection 'Microsoft.CognitiveServices/accounts/connections@2025-06-01' = if (!empty(searchEndpoint)) {
  parent: account
  name: 'search-connection'
  properties: {
    category: 'CognitiveSearch'
    target: searchEndpoint
    authType: 'AAD'
    isSharedToAll: true
  }
}

resource gptDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: gptModelName
  sku: {
    name: 'GlobalStandard'
    capacity: 30 // 30K TPM — sufficient for POC within budget
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: gptModelName
      version: gptModelVersion
    }
  }
}

// Deployment named 'gpt-4o-mini' but backed by gpt-4.1-mini — gpt-4o-mini 2024-07-18
// is in Deprecating state and blocked for new deployments in eastus as of 2026-08.
resource miniDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: 'gpt-4o-mini'
  dependsOn: [gptDeployment]
  sku: {
    name: 'GlobalStandard'
    capacity: 30
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4.1-mini'
      version: '2025-04-14'
    }
  }
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
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

// Grant managed identity "Cognitive Services User" (a97b65f3-...) -- despite
// the GUID being mislabeled "OpenAI User" in the old openai.bicep this
// replaces, `az role definition list` confirms a97b65f3-... is actually the
// broader "Cognitive Services User" role, not the narrower "Cognitive
// Services OpenAI User" (5e0bd9bd-...). The broad role already covers both
// OpenAI chat completions and Content Safety analyze-text on this
// multi-capability AIServices account, confirmed by both working live for
// the container's managed identity.
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource identityCognitiveServicesUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, identityPrincipalId, cognitiveServicesUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Same role for whoever is running `azd up`/`az deployment` -- without this,
// the installing user has no data-plane access to their own newly-provisioned
// resource (confirmed live: a 401 PermissionDenied calling chat completions
// as the deploying user, despite having created the resource) -- needed for
// local debugging, ad-hoc smoke tests, and any install-time script that calls
// this endpoint under the operator's own login rather than the managed identity.
resource deployerCognitiveServicesUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, deployer().objectId, cognitiveServicesUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: deployer().objectId
    principalType: 'User'
  }
}

// "Cognitive Services User" above only covers the classic per-resource data
// plane (audience https://cognitiveservices.azure.com, used by the direct
// REST/SDK calls in main.py/indexer.py/the .NET orchestrator). The
// azure-ai-evaluation SDK's evaluators request a token for the newer
// Foundry-project audience instead (https://ai.azure.com/.default, its
// TokenScope.COGNITIVE_SERVICES_MANAGEMENT) -- confirmed live: without this
// role, calls fail with 401 "Principal does not have access to API/Operation"
// even with Cognitive Services User already granted. "Azure AI Developer" is
// the role for that surface, scoped to the project (not just the account).
var azureAiDeveloperRoleId = '64702f94-c441-49e6-a78b-ef80e0188fee'

resource identityAzureAiDeveloper 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, identityPrincipalId, azureAiDeveloperRoleId)
  scope: project
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', azureAiDeveloperRoleId)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource deployerAzureAiDeveloper 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, deployer().objectId, azureAiDeveloperRoleId)
  scope: project
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', azureAiDeveloperRoleId)
    principalId: deployer().objectId
    principalType: 'User'
  }
}

output endpoint string = account.properties.endpoint
output resourceId string = account.id
output name string = account.name
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output projectName string = project.name
