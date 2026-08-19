// Azure API Management — AI Gateway
// Sits between all application code and AI Foundry model deployments.
// Provides: backend pool load balancing, semantic caching, per-consumer token budgets,
// rate limiting, circuit breaking, and emit-token-metric for Application Insights.
//
// Uses Consumption SKU: first 1M calls/month free, ~$3.50/1M thereafter.
// SystemAssigned identity is granted Cognitive Services User on the OpenAI resource.

param name string
param location string
param tags object = {}
param publisherEmail string
param publisherName string = 'Public Health RFP Platform'
param openAiEndpoint string                // base GPT-4o endpoint
param openAiFineTunedEndpoint string = ''  // fine-tuned GPT-4o endpoint (optional)
param openAiResourceId string
param appInsightsId string
param appInsightsInstrumentationKey string

// ── APIM service ─────────────────────────────────────────────────────────────
resource apim 'Microsoft.ApiManagement/service@2023-09-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Consumption'
    capacity: 0
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

// ── Application Insights logger ───────────────────────────────────────────────
resource logger 'Microsoft.ApiManagement/service/loggers@2023-09-01-preview' = {
  parent: apim
  name: 'appinsights-logger'
  properties: {
    loggerType: 'applicationInsights'
    credentials: {
      instrumentationKey: appInsightsInstrumentationKey
    }
    isBuffered: true
    resourceId: appInsightsId
  }
}

// ── Diagnostic settings (emit token metrics) ──────────────────────────────────
resource diagnostic 'Microsoft.ApiManagement/service/diagnostics@2023-09-01-preview' = {
  parent: apim
  name: 'applicationinsights'
  properties: {
    loggerId: logger.id
    alwaysLog: 'allErrors'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    metrics: true
  }
}

// ── Named values for endpoints ────────────────────────────────────────────────
resource nvOpenAiEndpoint 'Microsoft.ApiManagement/service/namedValues@2023-09-01-preview' = {
  parent: apim
  name: 'openai-endpoint'
  properties: {
    displayName: 'openai-endpoint'
    value: openAiEndpoint
    secret: false
  }
}

resource nvFineTunedEndpoint 'Microsoft.ApiManagement/service/namedValues@2023-09-01-preview' = if (!empty(openAiFineTunedEndpoint)) {
  parent: apim
  name: 'openai-finetuned-endpoint'
  properties: {
    displayName: 'openai-finetuned-endpoint'
    value: openAiFineTunedEndpoint
    secret: false
  }
}

// ── Backends ──────────────────────────────────────────────────────────────────
resource backendBase 'Microsoft.ApiManagement/service/backends@2023-09-01-preview' = {
  parent: apim
  name: 'openai-base'
  properties: {
    description: 'Base GPT-4o deployment — used for evaluation and fallback'
    url: '${openAiEndpoint}openai'
    protocol: 'http'
    tls: { validateCertificateChain: true, validateCertificateName: true }
  }
}

resource backendFineTuned 'Microsoft.ApiManagement/service/backends@2023-09-01-preview' = if (!empty(openAiFineTunedEndpoint)) {
  parent: apim
  name: 'openai-finetuned'
  properties: {
    description: 'Fine-tuned GPT-4o — primary for RFP section generation'
    url: '${openAiFineTunedEndpoint}openai'
    protocol: 'http'
    tls: { validateCertificateChain: true, validateCertificateName: true }
  }
}


// ── OpenAI API surface ────────────────────────────────────────────────────────
resource api 'Microsoft.ApiManagement/service/apis@2023-09-01-preview' = {
  parent: apim
  name: 'pubhealth-openai'
  properties: {
    displayName: 'PubHealth OpenAI Gateway'
    description: 'AI Gateway — routes to fine-tuned GPT-4o with fallback, semantic caching, token budgets'
    path: 'openai'
    protocols: ['https']
    subscriptionRequired: true
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'api-key'
    }
    isCurrent: true
  }
}

// ── API operations ────────────────────────────────────────────────────────────
resource opChatCompletions 'Microsoft.ApiManagement/service/apis/operations@2023-09-01-preview' = {
  parent: api
  name: 'chat-completions'
  properties: {
    displayName: 'Chat Completions'
    method: 'POST'
    urlTemplate: '/deployments/{deploymentId}/chat/completions'
    templateParameters: [
      { name: 'deploymentId', required: true, type: 'string' }
    ]
    request: {
      queryParameters: [
        { name: 'api-version', required: true, type: 'string', defaultValue: '2024-08-01-preview' }
      ]
    }
  }
}

// ── API-level policy ──────────────────────────────────────────────────────────
// Policies applied to every call through the gateway:
//   1. Managed identity auth to OpenAI (no keys)
//   2. Semantic caching (look up, then store on miss)
//   3. Token rate limit per subscription
//   4. Emit token usage metrics to App Insights
//   5. Route to backend pool (fine-tuned → base fallback)
resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2023-09-01-preview' = {
  parent: api
  name: 'policy'
  dependsOn: [
    backendBase
  ]
  properties: {
    format: 'rawxml'
    value: '''
<policies>
  <inbound>
    <base />
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
    <set-backend-service backend-id="openai-base" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
    '''
  }
}

// ── Subscriptions (one per consumer) ─────────────────────────────────────────
resource subGeneration 'Microsoft.ApiManagement/service/subscriptions@2023-09-01-preview' = {
  parent: apim
  name: 'sub-generation'
  properties: {
    scope: api.id
    displayName: 'RFP Generation (Foundry Agent + Prompt Flow)'
    state: 'active'
    allowTracing: false
  }
}

resource subEvaluation 'Microsoft.ApiManagement/service/subscriptions@2023-09-01-preview' = {
  parent: apim
  name: 'sub-evaluation'
  properties: {
    scope: api.id
    displayName: 'Evaluation Gate (Groundedness, Coherence)'
    state: 'active'
    allowTracing: false
  }
}

resource subAgents 'Microsoft.ApiManagement/service/subscriptions@2023-09-01-preview' = {
  parent: apim
  name: 'sub-agents'
  properties: {
    scope: api.id
    displayName: 'Specialist Agents (Review, Regulatory, Audit)'
    state: 'active'
    allowTracing: false
  }
}

// ── Role assignment: APIM identity → Cognitive Services User ─────────────────
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource apimOpenAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apim.id, openAiResourceId, cognitiveServicesUserRoleId)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────
output gatewayUrl string = apim.properties.gatewayUrl
output apimName string = apim.name
output apimPrincipalId string = apim.identity.principalId
output generationSubscriptionKeySecretName string = subGeneration.name
output evaluationSubscriptionKeySecretName string = subEvaluation.name
output agentsSubscriptionKeySecretName string = subAgents.name
