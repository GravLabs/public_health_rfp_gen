using Azure.Identity;
using Azure.Search.Documents;
using Microsoft.SemanticKernel;
using PubHealthRfp.Orchestrator.Models;
using PubHealthRfp.Orchestrator.Services;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen();

// AI Foundry tracing via Azure Monitor OpenTelemetry
var appInsightsConnStr = builder.Configuration["APPLICATIONINSIGHTS_CONNECTION_STRING"];
if (!string.IsNullOrEmpty(appInsightsConnStr))
{
    builder.Services.AddOpenTelemetry().UseAzureMonitor(o => o.ConnectionString = appInsightsConnStr);
}

var credential = new DefaultAzureCredential();

// Azure AI Search client
var searchEndpoint = new Uri(builder.Configuration["AZURE_SEARCH_ENDPOINT"]
    ?? throw new InvalidOperationException("AZURE_SEARCH_ENDPOINT is required"));
builder.Services.AddSingleton(_ => new SearchClient(searchEndpoint, "pubhealth-rfp-index", credential));

// Semantic Kernel with Azure OpenAI
var openAiEndpoint = builder.Configuration["AZURE_OPENAI_ENDPOINT"]
    ?? throw new InvalidOperationException("AZURE_OPENAI_ENDPOINT is required");
var chatDeployment = builder.Configuration["AZURE_OPENAI_GPT_DEPLOYMENT"] ?? "gpt-4o";
var miniDeployment = builder.Configuration["AZURE_OPENAI_MINI_DEPLOYMENT"] ?? "gpt-4o-mini";

builder.Services.AddSingleton(sp =>
{
    var kernelBuilder = Kernel.CreateBuilder();
    // Primary: fine-tuned GPT-4o for creative/grounded sections
    kernelBuilder.AddAzureOpenAIChatCompletion(
        deploymentName: chatDeployment,
        endpoint: openAiEndpoint,
        credentials: credential,
        serviceId: "gpt4o");
    // Secondary: GPT-4o-mini for boilerplate sections (~15x cheaper)
    kernelBuilder.AddAzureOpenAIChatCompletion(
        deploymentName: miniDeployment,
        endpoint: openAiEndpoint,
        credentials: credential,
        serviceId: "mini");
    return kernelBuilder.Build();
});

builder.Services.AddScoped<RfpOrchestrationService>();
builder.Services.AddHealthChecks();

var app = builder.Build();

app.UseSwagger();
app.UseSwaggerUI();
app.MapHealthChecks("/health");

app.MapPost("/generate", async (
    RfpRequest request,
    RfpOrchestrationService orchestrator,
    CancellationToken ct) =>
{
    var validationResults = new List<System.ComponentModel.DataAnnotations.ValidationResult>();
    if (!System.ComponentModel.DataAnnotations.Validator.TryValidateObject(
        request, new System.ComponentModel.DataAnnotations.ValidationContext(request), validationResults, true))
    {
        return Results.ValidationProblem(
            validationResults.ToDictionary(v => v.MemberNames.FirstOrDefault() ?? "error", v => new[] { v.ErrorMessage ?? "" }));
    }

    var draft = await orchestrator.GenerateDraftAsync(request, ct);
    return Results.Ok(draft);
})
.WithName("GenerateRfpDraft")
.WithOpenApi();

// Streaming endpoint — yields NDJSON section events as each section completes,
// allowing the Teams bot to update the progress card in real time.
app.MapPost("/generate/stream", async (
    RfpRequest request,
    RfpOrchestrationService orchestrator,
    HttpContext httpContext,
    CancellationToken ct) =>
{
    httpContext.Response.ContentType = "application/x-ndjson";
    httpContext.Response.Headers.CacheControl = "no-cache";

    var jsonOpts = new System.Text.Json.JsonSerializerOptions
    {
        PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.CamelCase
    };

    await foreach (var evt in orchestrator.GenerateSectionsStreamAsync(request, ct))
    {
        var line = System.Text.Json.JsonSerializer.Serialize(evt, jsonOpts) + "\n";
        await httpContext.Response.WriteAsync(line, ct);
        await httpContext.Response.Body.FlushAsync(ct);
    }
})
.WithName("GenerateRfpStream")
.WithOpenApi();

app.MapGet("/health/ready", () => Results.Ok(new { status = "ready", timestamp = DateTimeOffset.UtcNow }))
   .WithName("Readiness");

app.Run();
