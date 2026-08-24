param logAnalyticsName string
param appInsightsName string
param location string
param tags object = {}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
  }
}

// Generation metrics dashboard — cost/tokens per draft, gate pass/fail rate,
// eval score trends. Reads AppRequests.Properties, populated by
// record_generation_span/record_evaluation_span in src/api/observability.py
// (wired into src/api/main.py's generate_and_evaluate). Filtered to
// AppRoleName == 'pubhealth-rfp-api' (set via OTEL_SERVICE_NAME below) so
// this doesn't pick up the .NET orchestrator's own unrelated request
// telemetry from the same shared App Insights resource. All four queries
// validated against live data before being embedded here.
var workbookContent = {
  version: 'Notebook/1.0'
  items: [
    {
      type: 1
      content: {
        json: '## Public Health RFP Generation — Metrics\n\nCost, token usage, gate pass/fail rate, and evaluation scores for the RFP generation pipeline (`POST /generate-and-evaluate`).'
      }
      name: 'text - title'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'AppRequests | where AppRoleName == \'pubhealth-rfp-api\' and isnotempty(Properties[\'pubhealth.draft_id\']) | project TimeGenerated, draft_id=tostring(Properties[\'pubhealth.draft_id\']), program_area=tostring(Properties[\'pubhealth.program_area\']), cost_usd=todouble(Properties[\'llm.estimated_cost_usd\']) | order by TimeGenerated asc'
        size: 0
        timeContext: { durationMs: 604800000 }
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        crossComponentResources: [ logAnalytics.id ]
        visualization: 'linechart'
      }
      name: 'query - cost per draft'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'AppRequests | where AppRoleName == \'pubhealth-rfp-api\' and isnotempty(Properties[\'pubhealth.gate_decision\']) | summarize count() by gate=tostring(Properties[\'pubhealth.gate_decision\'])'
        size: 0
        timeContext: { durationMs: 604800000 }
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        crossComponentResources: [ logAnalytics.id ]
        visualization: 'piechart'
      }
      name: 'query - gate pass fail rate'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'AppRequests | where AppRoleName == \'pubhealth-rfp-api\' and isnotempty(Properties[\'eval.completeness\']) | project TimeGenerated, completeness=todouble(Properties[\'eval.completeness\']), parameter_accuracy=todouble(Properties[\'eval.parameter_accuracy\']), compliance=todouble(Properties[\'eval.compliance\']), groundedness=todouble(Properties[\'eval.groundedness\']), coherence=todouble(Properties[\'eval.coherence\']) | order by TimeGenerated asc'
        size: 0
        timeContext: { durationMs: 604800000 }
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        crossComponentResources: [ logAnalytics.id ]
        visualization: 'linechart'
      }
      name: 'query - eval score trends'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'AppRequests | where AppRoleName == \'pubhealth-rfp-api\' and isnotempty(Properties[\'llm.total_tokens\']) | summarize total_tokens=sum(toint(Properties[\'llm.total_tokens\'])), total_cost_usd=sum(todouble(Properties[\'llm.estimated_cost_usd\'])) by bin(TimeGenerated, 1h) | order by TimeGenerated asc'
        size: 0
        timeContext: { durationMs: 604800000 }
        queryType: 0
        resourceType: 'microsoft.operationalinsights/workspaces'
        crossComponentResources: [ logAnalytics.id ]
        visualization: 'barchart'
      }
      name: 'query - token usage trend'
    }
  ]
  styleSettings: {}
  '$schema': 'https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json'
}

resource generationWorkbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(resourceGroup().id, 'pubhealth-generation-metrics-workbook')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: 'Public Health RFP — Generation Metrics'
    serializedData: string(workbookContent)
    category: 'workbook'
    sourceId: appInsights.id
  }
}

output logAnalyticsId string = logAnalytics.id
output logAnalyticsWorkspaceId string = logAnalytics.properties.customerId
output appInsightsId string = appInsights.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output instrumentationKey string = appInsights.properties.InstrumentationKey
output workbookId string = generationWorkbook.id
