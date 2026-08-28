// Grants Cost Management Reader on the resource group so the API
// container's budget_monitor.py can query actual/forecasted spend
// (Microsoft.CostManagement/query) instead of silently falling back to
// session-only cost estimates.
//
// Split into its own module (rather than a bare resource in main.bicep,
// or a resource inside identity.bicep referencing its own UAI's
// principalId) because a role assignment's `name` must be resolvable at
// the start of deployment -- a plain string *parameter* satisfies that,
// but a cross-module output referenced directly in a same-scope
// resource's `name` does not (BCP120), and main.bicep's targetScope is
// `subscription`, so a bare `Microsoft.Authorization/roleAssignments`
// resource there can't be scoped to the resource group without a module
// (BCP139) either.
param principalId string

var costManagementReaderRoleId = '72fafb9e-0641-4937-9268-a91bfd8191a3'

resource costManagementReaderRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, principalId, costManagementReaderRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', costManagementReaderRoleId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
