// Azure API Management — AI Gateway
// Sits between all application code and the AI Foundry model deployments.
// Provides: semantic caching, per-caller token budgets, and token-usage
// metrics emitted to Application Insights — all Standard v2-only policies,
// see the header note on `apiPolicy` below for why this needed the SKU
// upgrade from the original Consumption tier.
//
// Auth: no API keys anywhere in this project, and this gateway is no
// exception -- callers present the exact same Entra bearer token they
// already fetch via DefaultAzureCredential for the Foundry resource
// (audience https://cognitiveservices.azure.com); `validate-jwt` checks it's
// actually this app's own managed identity before APIM authenticates to the
// backend on the caller's behalf via its own SystemAssigned identity.

param name string
param location string
param tags object = {}
param publisherEmail string
param publisherName string = 'Public Health RFP Platform'
param openAiEndpoint string
param openAiResourceId string
param appInsightsId string
param appInsightsInstrumentationKey string
param tenantId string
param allowedClientId string // the shared user-assigned identity's clientId (identity.bicep) -- the only caller this gateway accepts

// ── APIM service ─────────────────────────────────────────────────────────────
resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'StandardV2'
    capacity: 1
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
resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
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
resource diagnostic 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = {
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

// ── Backend ───────────────────────────────────────────────────────────────────
// One backend -- the unified AI Foundry account (see infra/modules/foundry.bicep)
// serves gpt-4o, gpt-4o-mini, and text-embedding-3-small off the same resource,
// so there's nothing to pool/route between the way the pre-Foundry-migration
// design assumed (a fine-tuned-vs-base split that was never actually built).
resource backend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  parent: apim
  name: 'openai'
  properties: {
    description: 'Unified AI Foundry account -- chat completions and embeddings'
    url: '${openAiEndpoint}openai'
    protocol: 'http'
    tls: { validateCertificateChain: true, validateCertificateName: true }
  }
}

// ── OpenAI API surface ────────────────────────────────────────────────────────
resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'pubhealth-openai'
  properties: {
    displayName: 'PubHealth OpenAI Gateway'
    description: 'AI Gateway — semantic caching, token budgets, usage metrics in front of the Foundry account'
    path: 'openai'
    protocols: ['https']
    subscriptionRequired: false
    isCurrent: true
  }
}

// ── API operations ────────────────────────────────────────────────────────────
resource opChatCompletions 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
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

resource opEmbeddings 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'embeddings'
  properties: {
    displayName: 'Embeddings'
    method: 'POST'
    urlTemplate: '/deployments/{deploymentId}/embeddings'
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
//   1. validate-jwt -- caller must present a valid Entra token for this app's
//      own managed identity (no API keys anywhere in this project)
//   2. Managed identity auth from APIM to the Foundry backend
//   3. Semantic caching (look up, then store on miss) -- Standard v2 only,
//      removed on the Consumption tier in commit 4543659, restored here
//   4. Token-per-minute budget -- also Standard v2 only, same history
//   5. Emit token usage metrics to Application Insights -- ditto
// No more choose/when routing: one backend now, nothing to route between.
// Bicep's triple-quoted strings are verbatim -- ${...} is NOT interpolated
// inside them (that's the whole point: embedding raw XML/JSON safely). The
// tenant and client ID are therefore templated in via token replacement
// below rather than string interpolation.
var openIdConfigUrl = '${environment().authentication.loginEndpoint}${tenantId}/v2.0/.well-known/openid-configuration'

var apiPolicyXmlTemplate = '''
<policies>
  <inbound>
    <base />
    <validate-jwt header-name="Authorization" failed-validation-httpcode="401" failed-validation-error-message="Unauthorized">
      <openid-config url="__OPENID_CONFIG_URL__" />
      <audiences>
        <audience>https://cognitiveservices.azure.com</audience>
        <audience>https://cognitiveservices.azure.com/</audience>
      </audiences>
      <required-claims>
        <claim name="appid" match="any">
          <value>__ALLOWED_CLIENT_ID__</value>
        </claim>
      </required-claims>
    </validate-jwt>
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
    <azure-openai-semantic-caching-lookup
      score-threshold="0.05"
      embeddings-backend-id="openai"
      embeddings-backend-auth="system-assigned"
      ignore-system-prompt="false"
      max-message-count="10" />
    <azure-openai-token-limit
      tokens-per-minute="500000"
      counter-key="@(context.Request.IpAddress)"
      estimate-prompt-tokens="true"
      tokens-consumed-header-name="x-tokens-consumed"
      remaining-tokens-header-name="x-tokens-remaining" />
    <set-backend-service backend-id="openai" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
    <azure-openai-semantic-caching-store duration="3600" />
    <azure-openai-emit-token-metric namespace="PubHealthRfp">
      <dimension name="deployment" value="@(context.Request.MatchedParameters["deploymentId"])" />
    </azure-openai-emit-token-metric>
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
'''

resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  dependsOn: [
    backend
  ]
  properties: {
    format: 'rawxml'
    value: replace(replace(apiPolicyXmlTemplate, '__OPENID_CONFIG_URL__', openIdConfigUrl), '__ALLOWED_CLIENT_ID__', allowedClientId)
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
