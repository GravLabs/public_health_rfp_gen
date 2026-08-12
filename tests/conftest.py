"""
Shared pytest fixtures and configuration.
Tests are unit tests — all Azure SDK calls are mocked.
No real Azure credentials are required to run the test suite.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add source directories to path for all tests
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "api"))
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "evaluation"))
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "ingestion"))


@pytest.fixture(scope="session", autouse=True)
def mock_azure_credentials():
    """Session-wide credential mock — prevents any real Azure auth attempts."""
    with patch("azure.identity.DefaultAzureCredential") as mock_cred, \
         patch("azure.identity.get_bearer_token_provider", return_value=lambda _: lambda: "test-token"):
        mock_cred.return_value = MagicMock()
        yield mock_cred


@pytest.fixture
def sample_rfp_sections() -> dict[str, str]:
    """A minimal but compliant set of RFP sections for evaluation tests."""
    base_compliance = (
        "All activities must comply with 2 CFR Part 200 Uniform Guidance. "
        "CLIA high-complexity certification required. Federal funds may not supplant existing state funding. "
        "No profit shall be derived from this cooperative agreement. "
        "Human subjects research must comply with 45 CFR Part 46. "
        "Indirect costs at federally negotiated rate only. "
        "All expenses must be allowable, allocable, and reasonable. "
        "Cost sharing is not required for this award."
    )
    return {
        "background": ("Public health laboratory surveillance is critical for emergency response. "
                       "COVID-19 demonstrated gaps in state laboratory surge capacity. " * 30 + base_compliance),
        "funding_parameters": ("Total funding: $3,500,000. Awards: 15–20 laboratories. "
                               "Award range: $100,000–$250,000. Period: 24 months. No cost sharing. " * 15),
        "eligibility": ("Public Health Labs member state laboratory. CLIA high-complexity certification. "
                        "LRN-B membership required. Partnership with state health department. " * 15
                        + base_compliance),
        "scope_of_work": ("A. Implement WGS capability for all priority pathogens within 12 months. "
                          "Achieve 500 genomes per week surge capacity. "
                          "B. Establish 24/7 emergency activation protocol with 2-hour response. "
                          "C. Submit all isolates to PulseNet within 5 business days. "
                          "D. Participate in annual CDC surge exercise. " * 30),
        "reporting_requirements": ("Monthly WGS submission reports to CDC. "
                                   "Quarterly progress reports. Annual exercise reports. "
                                   "Final report 90 days post-period of performance. " * 15),
        "budget_requirements": ("WGS library preparation reagents allowable. "
                                "Cloud compute allowable. Bioinformatician FTE (0.5–1.0) allowable. "
                                "2 CFR Part 200 compliance required. " * 15),
        "evaluation_criteria": ("Technical approach: 25 points. WGS capacity: 25 points. "
                                "Bioinformatics pipeline: 25 points. Network participation: 15 points. "
                                "Budget justification: 10 points. " * 10),
        "submission_instructions": ("Deadline: May 15, 2024, 5:00 PM Eastern Time. "
                                    "Submit via Public Health Labs Grants Portal. "
                                    "Required: Narrative (20 pages), Budget, WGS history, bioinformatician CV. " * 10),
    }


@pytest.fixture
def sample_rfp_params() -> dict:
    return {
        "total_funding": 3_500_000,
        "period_of_performance_months": 24,
        "cost_sharing_required": False,
        "award_range_min": 100_000,
        "award_range_max": 250_000,
    }
