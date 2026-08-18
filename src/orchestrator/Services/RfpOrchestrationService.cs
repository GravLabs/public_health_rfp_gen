using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Azure.Search.Documents;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.SemanticKernel;
using Microsoft.SemanticKernel.ChatCompletion;
using Microsoft.SemanticKernel.Connectors.AzureOpenAI;
using PubHealthRfp.Orchestrator.Models;
using PubHealthRfp.Orchestrator.Plugins;

namespace PubHealthRfp.Orchestrator.Services;

public class RfpOrchestrationService(
    Kernel kernel,
    SearchClient searchClient,
    ILogger<RfpOrchestrationService> logger)
{
    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };

    private static readonly Dictionary<string, (string prompt, int minWords)> SectionPrompts = new()
    {
        [SectionType.Background] = (
            "Write the Background and Purpose section for a public health laboratory RFP. " +
            "Ground the narrative in the specific program area, citing real CDC/Public Health Labs programs, surveillance networks, " +
            "and post-COVID public health priorities. Include: the public health problem, why state laboratories are the right responders, " +
            "what gap this funding addresses. Minimum 200 words. Use formal federal cooperative agreement language.",
            200),

        [SectionType.FundingParameters] = (
            "Write the Funding Parameters section as a structured table and narrative. " +
            "Include ALL parameters: total funding available, estimated number of awards, award range (min-max), " +
            "period of performance (dates and months), federal sponsor with office, cost sharing, indirect costs. " +
            "Use exact dollar amounts from the request. Do not round or approximate.",
            100),

        [SectionType.Eligibility] = (
            "Write the Eligibility Criteria section as a bulleted list. " +
            "Must include: public health member laboratory requirement, CLIA high-complexity certification, " +
            "program-specific capability requirements, and any required partnership letters. " +
            "Add program-specific eligibility criteria based on the program area (e.g., LRN membership for bioterrorism, " +
            "NELAP for environmental, PulseNet participation for foodborne).",
            80),

        [SectionType.ScopeOfWork] = (
            "Write the Scope of Work section with 4 lettered subsections (A, B, C, D). " +
            "Each subsection must have a header and 3-5 specific, measurable deliverables with numeric targets, " +
            "turnaround times, or percentage thresholds. Use program-specific technical terminology. " +
            "Reference specific CDC platforms, reporting systems, or technical standards where applicable.",
            350),

        [SectionType.ReportingRequirements] = (
            "Write the Reporting Requirements section as a bulleted list. " +
            "Include: frequency (monthly/quarterly/semi-annual/annual), report type, recipient, " +
            "and due date for each requirement. Must include a final report 90 days post-period of performance.",
            80),

        [SectionType.BudgetRequirements] = (
            "Write the Budget Requirements section specifying allowable and unallowable costs. " +
            "Be specific about cost categories relevant to the program area (reagents, equipment, personnel, travel). " +
            "Reference 2 CFR Part 200 Uniform Guidance. Note indirect cost treatment and any caps or restrictions.",
            80),

        [SectionType.EvaluationCriteria] = (
            "Write the Evaluation Criteria section as a table with 5 criteria totaling 100 points. " +
            "Each criterion must have: name, point value, and 1-sentence description. " +
            "Criteria should reflect the program area priorities from the scope of work.",
            80),

        [SectionType.SubmissionInstructions] = (
            "Write the Submission Instructions section including: deadline (specific date and time Eastern), " +
            "submission portal (Public Health Labs Grants Portal), required documents list, page limits, and contact information. " +
            "Set deadline approximately 6 weeks after the issue date. List all required attachments.",
            80),
    };

    public async Task<RfpDraft> GenerateDraftAsync(RfpRequest request, CancellationToken cancellationToken = default)
    {
        var draftId = Guid.NewGuid().ToString("N")[..12];
        var rfpId = BuildRfpId(request);
        var groundingChunks = new List<GroundingChunk>();
        var sections = new Dictionary<string, string>();
        var totalPromptTokens = 0;
        var totalCompletionTokens = 0;

        logger.LogInformation("Starting RFP generation for {ProgramArea}, draft {DraftId}", request.ProgramArea, draftId);
        using var genActivity = FoundryTracingExtensions.StartGenerationActivity(rfpId, request.ProgramArea);

        var searchPlugin = new SearchPlugin(searchClient);
        var contextJson = await searchPlugin.SearchAllSectionsAsync(request.ProgramArea, cancellationToken);
        var groundingContext = BuildGroundingContext(request, contextJson, groundingChunks);

        foreach (var sectionKey in SectionType.All)
        {
            logger.LogDebug("Generating section: {Section}", sectionKey);

            var sectionContext = await searchPlugin.SearchCorpusAsync(sectionKey, request.ProgramArea, cancellationToken: cancellationToken);
            var (sectionText, promptTokens, completionTokens) = await GenerateSectionAsync(
                sectionKey, request, groundingContext, sectionContext, cancellationToken);

            sections[sectionKey] = sectionText;
            totalPromptTokens += promptTokens;
            totalCompletionTokens += completionTokens;
            genActivity?.RecordSectionGeneration(sectionKey, !string.IsNullOrWhiteSpace(sectionText));
        }

        genActivity?.RecordTokenUsage(totalPromptTokens, totalCompletionTokens);
        genActivity?.RecordGroundingChunks(groundingChunks.Count, groundingChunks.Count > 0 ? groundingChunks.Max(c => c.Score) : 0);
        logger.LogInformation("Draft {DraftId} generated. Tokens: {Total}", draftId, totalPromptTokens + totalCompletionTokens);

        return new RfpDraft
        {
            DraftId = draftId,
            RfpId = rfpId,
            ProgramArea = request.ProgramArea,
            FederalSponsor = request.FederalSponsor,
            Sections = sections,
            GroundingChunks = groundingChunks,
            TokenUsage = new TokenUsage
            {
                PromptTokens = totalPromptTokens,
                CompletionTokens = totalCompletionTokens
            }
        };
    }

    // Boilerplate sections: templated content, no creative grounding needed — use gpt-4o-mini.
    // Creative/grounded sections (Background, ScopeOfWork, EvaluationCriteria, FundingParameters,
    // Eligibility) use the fine-tuned GPT-4o for domain accuracy.
    private static readonly HashSet<string> MiniSections =
    [
        SectionType.ReportingRequirements,
        SectionType.BudgetRequirements,
        SectionType.SubmissionInstructions,
    ];

    private async Task<(string text, int promptTokens, int completionTokens)> GenerateSectionAsync(
        string sectionKey,
        RfpRequest request,
        string groundingContext,
        string sectionContext,
        CancellationToken cancellationToken)
    {
        var (sectionPrompt, _) = SectionPrompts[sectionKey];

        // System message is stable across all 8 section calls for this request.
        // RFP parameters + shared grounding context live here so Azure OpenAI prompt
        // caching kicks in for sections 2–8 (identical prefix = cache hit, ~50% off prompt tokens).
        var systemMessage = $"""
            You are an expert public health laboratory grants writer with deep knowledge of CDC cooperative agreements,
            Public Health Labs programs, and federal grant compliance requirements (2 CFR Part 200, CLIA, select agent regulations).
            Generate precise, grounded RFP content that exactly matches the parameters provided.
            Do not hallucinate program names, regulatory citations, or funding amounts.
            Always use language appropriate for a federal cooperative agreement, not a research grant.

            RFP PARAMETERS (authoritative — use exact values):
            - Program Area: {request.ProgramArea}
            - Federal Sponsor: {request.FederalSponsor}
            - Total Funding: ${request.TotalFunding:N0}
            - Period of Performance: {request.PeriodOfPerformanceMonths} months
            - Awards: {request.EstimatedAwardsMin}–{request.EstimatedAwardsMax}
            - Award Range: ${request.AwardRangeMin:N0}–${request.AwardRangeMax:N0}
            - Cost Sharing: {(request.CostSharingRequired ? "Required" : "Not required")}
            - Fiscal Year: {request.FiscalYear ?? "2024"}
            - Key Requirements: {string.Join("; ", request.KeyRequirements)}

            GROUNDING CONTEXT FROM SIMILAR RFPs (inform style and technical content):
            {groundingContext}
            """;

        // User message contains only the per-section variable content.
        var userMessage = $"""
            TASK: {sectionPrompt}

            SECTION-SPECIFIC CORPUS EXCERPTS:
            {sectionContext}

            SECTION TO GENERATE: {sectionKey.Replace("_", " ").ToUpperInvariant()}

            Write only the section content. Do not include headers — just the content text.
            Be specific, use exact numbers from the parameters, and ground all technical claims in the corpus context.
            """;

        var serviceId = MiniSections.Contains(sectionKey) ? "mini" : "gpt4o";
        var chat = kernel.Services.GetKeyedService<IChatCompletionService>(serviceId)
            ?? kernel.GetRequiredService<IChatCompletionService>();

        var history = new ChatHistory();
        history.AddSystemMessage(systemMessage);
        history.AddUserMessage(userMessage);

        var settings = new AzureOpenAIPromptExecutionSettings
        {
            MaxTokens = 1200,
            Temperature = 0.2,
        };

        var response = await chat.GetChatMessageContentAsync(history, settings, kernel, cancellationToken);

        var usage = response.Metadata?["Usage"] as OpenAI.Chat.ChatTokenUsage;
        return (
            response.Content ?? string.Empty,
            usage?.InputTokenCount ?? 0,
            usage?.OutputTokenCount ?? 0
        );
    }

    private static string BuildGroundingContext(RfpRequest request, string contextJson, List<GroundingChunk> chunks)
    {
        try
        {
            var results = JsonSerializer.Deserialize<List<JsonElement>>(contextJson) ?? [];
            var sb = new StringBuilder();
            foreach (var result in results.Take(5))
            {
                var rfpId = result.GetProperty("rfp_id").GetString() ?? "";
                var sectionType = result.GetProperty("section_type").GetString() ?? "";
                var content = result.GetProperty("content").GetString() ?? "";
                var score = result.GetProperty("score").GetDouble();

                chunks.Add(new GroundingChunk
                {
                    ChunkId = $"{rfpId}_{sectionType}",
                    RfpId = rfpId,
                    SectionType = sectionType,
                    Score = score,
                    ContentPreview = content[..Math.Min(200, content.Length)]
                });

                sb.AppendLine($"[RFP: {rfpId} | Section: {sectionType}]");
                sb.AppendLine(content[..Math.Min(500, content.Length)]);
                sb.AppendLine();
            }
            return sb.ToString();
        }
        catch
        {
            return $"Program area: {request.ProgramArea}. No similar RFPs retrieved.";
        }
    }

    // ── Streaming generation ──────────────────────────────────────────────────

    public record SectionStreamEvent(
        string Type,
        string? SectionKey = null,
        string? SectionText = null,
        int Index = 0,
        int Total = 8,
        int PromptTokens = 0,
        int CompletionTokens = 0
    );

    public async IAsyncEnumerable<SectionStreamEvent> GenerateSectionsStreamAsync(
        RfpRequest request,
        [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        var sectionKeys = SectionType.All;
        var total = sectionKeys.Count;
        var groundingChunks = new List<GroundingChunk>();

        yield return new SectionStreamEvent("started", Total: total);

        var searchPlugin = new SearchPlugin(searchClient);
        var contextJson = await searchPlugin.SearchAllSectionsAsync(request.ProgramArea, cancellationToken);
        var groundingContext = BuildGroundingContext(request, contextJson, groundingChunks);

        for (var i = 0; i < sectionKeys.Count; i++)
        {
            var sectionKey = sectionKeys[i];
            var sectionContext = await searchPlugin.SearchCorpusAsync(
                sectionKey, request.ProgramArea, cancellationToken: cancellationToken);
            var (sectionText, promptTokens, completionTokens) = await GenerateSectionAsync(
                sectionKey, request, groundingContext, sectionContext, cancellationToken);

            yield return new SectionStreamEvent("section", sectionKey, sectionText, i + 1, total, promptTokens, completionTokens);
        }
    }

    private static string BuildRfpId(RfpRequest request)
    {
        var year = request.FiscalYear ?? DateTime.UtcNow.Year.ToString();
        var area = request.ProgramArea
            .Split(' ', '-', '/')
            .Take(2)
            .Select(w => w.ToUpperInvariant()[..Math.Min(4, w.Length)])
            .Aggregate(string.Concat);
        return $"Public Health Labs-RFP-{year}-{area}-GEN";
    }
}
