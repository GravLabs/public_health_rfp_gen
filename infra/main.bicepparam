using './main.bicep'

param environmentName = readEnvironmentVariable('AZURE_ENV_NAME', 'dev')
param location = readEnvironmentVariable('AZURE_LOCATION', 'eastus')
param apimPublisherEmail = readEnvironmentVariable('AZURE_PUBLISHER_EMAIL', 'admin@example.com')
param ownerEmail = readEnvironmentVariable('AZURE_OWNER_EMAIL', 'owner@example.com')

// Model configuration
param openAiModelName = 'gpt-4o'
param openAiModelVersion = '2025-01-01'
param embeddingModelName = 'text-embedding-3-small'

// Cost controls
param budgetAmountUsd = 500

// Search configuration - Basic tier to stay within $500/mo budget
param searchSkuName = 'basic'

// Tags applied to all resources
param tags = {
  project: 'pubhealth-rfp-poc'
  environment: readEnvironmentVariable('AZURE_ENV_NAME', 'dev')
  owner: readEnvironmentVariable('AZURE_OWNER_EMAIL', 'owner@example.com')
  costCenter: 'pubhealth-poc'
}
