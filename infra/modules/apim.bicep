// Azure API Management — AI Gateway
// Sits between all application code and the AI Foundry model deployments.
// Provides per-caller token budgets and token-usage metrics emitted to
// Application Insights -- Standard v2-only policies, hence the SKU upgrade
// from the original Consumption tier. (Semantic caching is deferred -- see
// the note above the policy template below.)
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
//   3. Token-per-minute budget (llm-token-limit) -- Standard v2 only,
//      removed on the Consumption tier in commit 4543659, restored here
//   4. Emit token usage metrics to Application Insights (llm-emit-token-metric)
//      -- ditto
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
        <!-- azure-ai-evaluation's internal prompty engine (GroundednessEvaluator/
             CoherenceEvaluator) requests a token for this audience specifically,
             not cognitiveservices.azure.com; confirmed live via a debug
             endpoint decoding the actual token claims: same appid (this app's
             identity), different aud. Without this, evaluator calls through
             the gateway get a 401 while everything else (generation, classify)
             succeeds, since those use cognitiveservices.azure.com. -->
        <audience>https://ai.azure.com</audience>
        <audience>https://ai.azure.com/</audience>
      </audiences>
      <required-claims>
        <claim name="appid" match="any">
          <value>__ALLOWED_CLIENT_ID__</value>
        </claim>
      </required-claims>
    </validate-jwt>
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
    <llm-token-limit
      tokens-per-minute="500000"
      counter-key="@(context.Request.IpAddress)"
      estimate-prompt-tokens="true"
      tokens-consumed-header-name="x-tokens-consumed"
      remaining-tokens-header-name="x-tokens-remaining" />
    <llm-emit-token-metric namespace="PubHealthRfp">
      <dimension name="deployment" value="@(context.Request.MatchedParameters["deploymentId"])" />
    </llm-emit-token-metric>
    <set-backend-service backend-id="openai" />
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
// Semantic caching (llm-semantic-cache-lookup/-store) is deliberately not
// wired up here: as of the current AI Gateway docs it requires an Azure
// Managed Redis instance with the RediSearch module enabled, registered as
// an APIM external cache -- a whole additional paid resource this POC
// doesn't provision. Deferred; see README POC Limitations.

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
  // NOTE: this name is necessarily based only on static values -- ARM
  // requires a role assignment's `name` to be resolvable at the start of
  // deployment, before a SystemAssigned identity's principalId exists, so
  // it can't be included here (BCP120). That means if APIM is ever deleted
  // and recreated, its new SystemAssigned identity gets a new principalId
  // but this resource keeps the SAME deterministic name, and ARM will
  // reject the redeploy with RoleAssignmentUpdateNotPermitted ("principal
  // ID ... not allowed to be updated"). Fix: find and delete the stale
  // assignment first -- `az role assignment list --resource-group <rg>
  // --role "Cognitive Services User"`, confirm its principalId no longer
  // resolves via `az ad sp show`, then `az role assignment delete --ids
  // <id>` -- before re-running azd provision.
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
