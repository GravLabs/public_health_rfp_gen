// Azure Bot Service — Teams Bot Channel Registration
// Free F0 tier; connects to the FastAPI /api/messages webhook.
// No M365 Copilot license required — standard Teams inclusion.

param name string
param location string = 'global'   // Bot Service is a global resource
param tags object = {}
param displayName string = 'Public Health RFP Bot'
param messagingEndpoint string     // e.g. https://<api-ca-url>/api/messages
param microsoftAppId string        // Client ID of the user-assigned managed identity

// ── Bot Channels Registration ─────────────────────────────────────────────────
resource botService 'Microsoft.BotService/botServices@2022-09-15' = {
  name: name
  location: location
  tags: tags
  kind: 'azurebot'
  sku: {
    name: 'F0'   // Free tier — sufficient for POC and low-volume production
  }
  properties: {
    displayName: displayName
    endpoint: messagingEndpoint
    msaAppId: microsoftAppId
    msaAppType: 'UserAssignedMSI'
    msaAppMSIResourceId: ''   // populated post-provision via azd env
    isStreamingSupported: false
    schemaTransformationVersion: '1.3'
  }
}

// ── Teams channel ─────────────────────────────────────────────────────────────
resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: botService
  name: 'MsTeamsChannel'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      enableCalling: false
      isEnabled: true
    }
  }
}

output botName string = botService.name
output botEndpoint string = botService.properties.endpoint
