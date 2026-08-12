using System.ComponentModel;
using System.Text.Json;
using Azure;
using Azure.Search.Documents;
using Azure.Search.Documents.Models;
using Microsoft.SemanticKernel;
using PubHealthRfp.Orchestrator.Models;

namespace PubHealthRfp.Orchestrator.Plugins;

public class SearchPlugin(SearchClient searchClient)
{
    private const int DefaultTopK = 6;
    private const string VectorField = "content_vector";

    [KernelFunction, Description("Search the Public Health RFP corpus for relevant grounding context on a specific section type and program area.")]
    public async Task<string> SearchCorpusAsync(
        [Description("The section type to search for (e.g. scope_of_work, eligibility, funding_parameters)")] string sectionType,
        [Description("The program area or topic to search for")] string programArea,
        [Description("Optional: specific RFP ID to include in results")] string? rfpId = null,
        CancellationToken cancellationToken = default)
    {
        var filter = rfpId is not null
            ? $"section_type eq '{sectionType}' and rfp_id eq '{rfpId}'"
            : $"section_type eq '{sectionType}'";

        var options = new SearchOptions
        {
            Filter = filter,
            Size = DefaultTopK,
            QueryType = SearchQueryType.Semantic,
            SemanticSearch = new SemanticSearchOptions
            {
                SemanticConfigurationName = "pubhealth-rfp-semantic",
                QueryCaption = new QueryCaptionOptions(QueryCaptionType.Extractive),
            },
            Select = { "content", "rfp_id", "section_type", "program_area", "federal_sponsor", "fiscal_year", "chunk_index", "context_summary" }
        };

        var results = await searchClient.SearchAsync<SearchDocument>(programArea, options, cancellationToken);

        var chunks = new List<object>();
        await foreach (var result in results.Value.GetResultsAsync())
        {
            chunks.Add(new
            {
                rfp_id = result.Document["rfp_id"]?.ToString(),
                section_type = result.Document["section_type"]?.ToString(),
                program_area = result.Document["program_area"]?.ToString(),
                federal_sponsor = result.Document["federal_sponsor"]?.ToString(),
                fiscal_year = result.Document["fiscal_year"]?.ToString(),
                context_summary = result.Document["context_summary"]?.ToString(),
                content = result.Document["content"]?.ToString(),
                score = result.Score
            });
        }

        return JsonSerializer.Serialize(chunks, new JsonSerializerOptions { WriteIndented = false });
    }

    [KernelFunction, Description("Search all section types for a specific program area to get a holistic view of comparable RFPs.")]
    public async Task<string> SearchAllSectionsAsync(
        [Description("The program area or topic")] string programArea,
        CancellationToken cancellationToken = default)
    {
        var options = new SearchOptions
        {
            Size = 10,
            QueryType = SearchQueryType.Semantic,
            SemanticSearch = new SemanticSearchOptions
            {
                SemanticConfigurationName = "pubhealth-rfp-semantic",
            },
            Select = { "content", "rfp_id", "section_type", "program_area", "federal_sponsor", "fiscal_year" }
        };

        var results = await searchClient.SearchAsync<SearchDocument>(programArea, options, cancellationToken);

        var chunks = new List<object>();
        await foreach (var result in results.Value.GetResultsAsync())
        {
            chunks.Add(new
            {
                rfp_id = result.Document["rfp_id"]?.ToString(),
                section_type = result.Document["section_type"]?.ToString(),
                content = result.Document["content"]?.ToString()?.Substring(0, Math.Min(400, result.Document["content"]?.ToString()?.Length ?? 0)),
                score = result.Score
            });
        }

        return JsonSerializer.Serialize(chunks, new JsonSerializerOptions { WriteIndented = false });
    }
}
