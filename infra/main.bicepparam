using './main.bicep'

param environmentName = readEnvironmentVariable('AZURE_ENV_NAME', 'dev')
param location = readEnvironmentVariable('AZURE_LOCATION', 'eastus')
param apimPublisherEmail = readEnvironmentVariable('APIM_PUBLISHER_EMAIL', 'admin@example.com')
param ownerEmail = readEnvironmentVariable('OWNER_EMAIL', 'owner@example.com')

// Bot App Registration must be created before azd up (Bot Service validates it
// against the tenant at provision time — see scripts/install.sh Phase 3).
param botAppId = readEnvironmentVariable('BOT_APP_ID', '')

// Model configuration
param openAiModelName = 'gpt-4o'
param openAiModelVersion = '2024-11-20'
param embeddingModelName = 'text-embedding-3-small'

// Cost controls
param budgetAmountUsd = 500

// Search configuration - Basic tier to stay within $500/mo budget
param searchSkuName = 'basic'

// Tags applied to all resources
param tags = {
  project: 'pubhealth-rfp-poc'
  environment: readEnvironmentVariable('AZURE_ENV_NAME', 'dev')
  owner: readEnvironmentVariable('OWNER_EMAIL', 'owner@example.com')
  costCenter: 'pubhealth-poc'
}
