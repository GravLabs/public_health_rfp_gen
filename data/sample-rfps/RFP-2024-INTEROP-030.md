# REQUEST FOR PROPOSALS
## Laboratory Data Interoperability Initiative — ELR Modernization and FHIR Implementation
**RFP ID:** Public Health Labs-RFP-2024-INTEROP-030  
**Issue Date:** May 6, 2024  
**Federal Sponsor:** Centers for Disease Control and Prevention (CDC) / CSELS  
**CDC Award Number:** 6NU50CK000591-06  
**Program Area:** Data Modernization — Laboratory Interoperability

---

## 1. Background and Purpose

Electronic Laboratory Reporting (ELR) is the mechanism by which laboratory results for reportable conditions are electronically transmitted from laboratories to health departments. Current ELR implementations rely primarily on HL7 v2.5.1 messaging that is now decades old, inconsistently implemented, and not interoperable with modern FHIR-based health information exchange standards. CDC's Data Modernization Initiative (DMI) calls for a transition to HL7 FHIR-based laboratory data exchange across the public health laboratory system.

This RFP funds state public health laboratories to implement HL7 FHIR R4-compliant electronic case reporting (eCR) and ELR, retiring legacy HL7 v2 interfaces for at least 5 priority reportable conditions, and integrate with state health information exchange (HIE) infrastructure to enable real-time public health laboratory result flows.

---

## 2. Funding Parameters

| Parameter | Value |
|---|---|
| Total Funding Available | $5,800,000 |
| Estimated Number of Awards | 22–28 |
| Award Range (per laboratory) | $100,000 – $400,000 |
| Period of Performance | 24 months (September 1, 2024 – August 31, 2026) |
| Federal Sponsor | CDC / CSELS / DPHSI / DMI |
| Cost Sharing Required | No |
| Indirect Costs | Allowable, up to negotiated rate |

---

## 3. Eligibility Criteria

- Public Health Labs member state or large local public health laboratory
- Current ELR sender for at least 5 reportable conditions
- Active relationship with state HIE or Health Information Organization (HIO)
- LIMS vendor relationship with a FHIR-capable roadmap (documentation required)
- IT staff or contract resources capable of FHIR interface development

---

## 4. Scope of Work

### A. FHIR ELR Implementation
- Retire HL7 v2 ELR interfaces and replace with HL7 FHIR R4 compliant messages for at least 5 priority reportable conditions (applicant selects; CDC/Public Health Labs-recommended priority list provided)
- Achieve Public Health Labs Informatics Messaging Services (AIMS) Platform onboarding within 9 months of award
- Pass CDC RCKMS (Reportable Conditions Knowledge Management System) conformance testing for implemented conditions

### B. Real-Time HIE Integration
- Implement bidirectional FHIR API connection with state HIE for at least 3 reportable condition result flows
- Achieve ≤15-minute latency from laboratory result finalization to HIE availability for priority conditions
- Pilot SMART on FHIR application for laboratory result access by authorized public health users

### C. Automated Routing and Decision Support
- Implement LOINC-based automated routing of FHIR messages to appropriate public health recipients (state epidemiology, local health departments)
- Develop and test business rules for automated completeness checking on incoming FHIR messages
- Reduce manually completed ELR records to <5% of total submissions

### D. Standards Compliance and Governance
- Participate in Public Health Labs FHIR laboratory interoperability community of practice
- Document and publish state laboratory FHIR implementation guide on public repository
- Achieve US Core conformance for all FHIR laboratory result resources

---

## 5. Reporting Requirements

- **Quarterly FHIR Implementation Milestones** — conditions retired from HL7 v2, AIMS onboarding status
- **Monthly ELR Completeness Metrics** — % electronic, % manual, latency by condition
- **AIMS Conformance Test Results** — within 30 days of testing
- **Final Report** — 90 days post-period of performance including FHIR IG publication link

---

## 6. Budget Requirements

LIMS FHIR module licensing or custom development, HIE integration technical work, AIMS platform onboarding costs allowable. Staff time for FHIR conformance testing, standards participation allowable. Minimum 0.5 FTE health informatics staff dedicated to project.

---

## 7. Evaluation Criteria

| Criterion | Points |
|---|---|
| FHIR Implementation Readiness | 25 |
| HIE Integration Plan | 25 |
| Scope of Condition Coverage | 20 |
| Standards Compliance Approach | 20 |
| Budget Justification | 10 |

---

## 8. Submission Instructions

**Deadline:** June 21, 2024, 5:00 PM Eastern Time. Submit via Public Health Labs Grants Portal. Required: Narrative (18 pages), Budget, LIMS vendor FHIR roadmap documentation, state HIE partnership letter, current ELR condition list.
