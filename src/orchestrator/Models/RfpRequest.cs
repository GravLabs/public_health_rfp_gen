using System.ComponentModel.DataAnnotations;
using System.Text.Json.Serialization;

namespace PubHealthRfp.Orchestrator.Models;

public record RfpRequest
{
    [Required]
    [JsonPropertyName("program_area")]
    public required string ProgramArea { get; init; }

    [Required]
    [JsonPropertyName("federal_sponsor")]
    public required string FederalSponsor { get; init; }

    [Required, Range(1, 200_000_000)]
    [JsonPropertyName("total_funding")]
    public required decimal TotalFunding { get; init; }

    [Required, Range(1, 120)]
    [JsonPropertyName("period_of_performance_months")]
    public required int PeriodOfPerformanceMonths { get; init; }

    [JsonPropertyName("fiscal_year")]
    public string? FiscalYear { get; init; }

    [JsonPropertyName("award_range_min")]
    public decimal? AwardRangeMin { get; init; }

    [JsonPropertyName("award_range_max")]
    public decimal? AwardRangeMax { get; init; }

    [JsonPropertyName("estimated_awards_min")]
    public int? EstimatedAwardsMin { get; init; }

    [JsonPropertyName("estimated_awards_max")]
    public int? EstimatedAwardsMax { get; init; }

    [JsonPropertyName("cost_sharing_required")]
    public bool CostSharingRequired { get; init; } = false;

    [JsonPropertyName("key_requirements")]
    public List<string> KeyRequirements { get; init; } = [];

    [JsonPropertyName("similar_rfp_ids")]
    public List<string> SimilarRfpIds { get; init; } = [];

    [JsonPropertyName("sharepoint_output_library")]
    public string? SharePointOutputLibrary { get; init; }
}
