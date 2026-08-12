namespace PubHealthRfp.Orchestrator.Models;

public static class SectionType
{
    public const string Background = "background";
    public const string FundingParameters = "funding_parameters";
    public const string Eligibility = "eligibility";
    public const string ScopeOfWork = "scope_of_work";
    public const string ReportingRequirements = "reporting_requirements";
    public const string BudgetRequirements = "budget_requirements";
    public const string EvaluationCriteria = "evaluation_criteria";
    public const string SubmissionInstructions = "submission_instructions";

    public static readonly IReadOnlyList<string> All =
    [
        Background, FundingParameters, Eligibility, ScopeOfWork,
        ReportingRequirements, BudgetRequirements, EvaluationCriteria, SubmissionInstructions
    ];
}
