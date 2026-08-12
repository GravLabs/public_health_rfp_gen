using System.Text.Json.Serialization;

namespace PubHealthRfp.Orchestrator.Models;

public record RfpDraft
{
    [JsonPropertyName("draft_id")]
    public required string DraftId { get; init; }

    [JsonPropertyName("rfp_id")]
    public required string RfpId { get; init; }

    [JsonPropertyName("program_area")]
    public required string ProgramArea { get; init; }

    [JsonPropertyName("federal_sponsor")]
    public required string FederalSponsor { get; init; }

    [JsonPropertyName("generated_at")]
    public DateTimeOffset GeneratedAt { get; init; } = DateTimeOffset.UtcNow;

    [JsonPropertyName("sections")]
    public required Dictionary<string, string> Sections { get; init; }

    [JsonPropertyName("grounding_chunks")]
    public List<GroundingChunk> GroundingChunks { get; init; } = [];

    [JsonPropertyName("token_usage")]
    public TokenUsage TokenUsage { get; init; } = new();

    [JsonPropertyName("sharepoint_url")]
    public string? SharePointUrl { get; init; }

    [JsonPropertyName("fabric_lakehouse_path")]
    public string? FabricLakehousePath { get; init; }
}

public record GroundingChunk
{
    [JsonPropertyName("chunk_id")]
    public required string ChunkId { get; init; }

    [JsonPropertyName("rfp_id")]
    public required string RfpId { get; init; }

    [JsonPropertyName("section_type")]
    public required string SectionType { get; init; }

    [JsonPropertyName("score")]
    public double Score { get; init; }

    [JsonPropertyName("content_preview")]
    public required string ContentPreview { get; init; }
}

public record TokenUsage
{
    [JsonPropertyName("prompt_tokens")]
    public int PromptTokens { get; init; }

    [JsonPropertyName("completion_tokens")]
    public int CompletionTokens { get; init; }

    [JsonPropertyName("total_tokens")]
    public int TotalTokens => PromptTokens + CompletionTokens;
}
