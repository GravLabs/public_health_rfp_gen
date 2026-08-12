using System.Diagnostics;
using Microsoft.Extensions.Logging;

namespace PubHealthRfp.Orchestrator.Services;

/// <summary>
/// OpenTelemetry activity helpers for AI Foundry tracing.
/// Emits structured spans for generation operations, consumed by Azure Monitor / AI Foundry traces.
/// </summary>
public static class FoundryTracingExtensions
{
    private static readonly ActivitySource ActivitySource = new("PubHealthRfp.Orchestrator");

    public static Activity? StartGenerationActivity(string rfpId, string programArea)
    {
        var activity = ActivitySource.StartActivity("rfp.generate");
        activity?.SetTag("pubhealth.rfp_id", rfpId);
        activity?.SetTag("pubhealth.program_area", programArea);
        activity?.SetTag("gen_ai.system", "azure_openai");
        return activity;
    }

    public static void RecordTokenUsage(this Activity? activity, int promptTokens, int completionTokens)
    {
        if (activity is null) return;
        activity.SetTag("gen_ai.usage.prompt_tokens", promptTokens);
        activity.SetTag("gen_ai.usage.completion_tokens", completionTokens);
        activity.SetTag("gen_ai.usage.total_tokens", promptTokens + completionTokens);

        const double promptRate = 0.0025;  // per 1K
        const double completionRate = 0.010;
        var cost = (promptTokens / 1000.0 * promptRate) + (completionTokens / 1000.0 * completionRate);
        activity.SetTag("gen_ai.usage.estimated_cost_usd", Math.Round(cost, 6));
    }

    public static void RecordSectionGeneration(this Activity? activity, string sectionKey, bool success)
    {
        activity?.AddEvent(new ActivityEvent($"section.{sectionKey}", tags: new ActivityTagsCollection
        {
            { "section_key", sectionKey },
            { "success", success }
        }));
    }

    public static void RecordGroundingChunks(this Activity? activity, int chunkCount, double topScore)
    {
        if (activity is null) return;
        activity.SetTag("rag.grounding_chunk_count", chunkCount);
        activity.SetTag("rag.top_chunk_score", Math.Round(topScore, 4));
    }
}
