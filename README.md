# NOVIQ — Clinical Coding Intelligence Platform

AI-powered clinical coding engine for Australian hospital revenue cycle workflows. NOVIQ reads a patient's complete EHR before claim submission and produces accurate, ethically justified **ICD-10-AM**, **ACHI**, and **AR-DRG** codes — protecting simultaneously against upcoding and revenue leakage.

**Core principle: Clinical Truth Preservation.** Every code the engine proposes is traceable to a specific line in the patient record, scored against the relevant Australian Coding Standard (ACS), and gated behind physician approval before submission.

Built by **Dr. Mohamed Kassab** — General Surgeon, Founder of Noviq Health, with a background spanning clinical surgery, healthcare operations, and insurance/TPA coding audit (MetLife, MedNet/Munich Re).

---

## What it does

1. **Ingests EHR documents** — PDF, TXT, HL7 v2, FHIR R4 XML, or a structured JSON episode — across the six standard document types (Initial Medical Report → Admission Report → Progress Notes → Operation Notes → Nursing Notes → Discharge Summary).
2. **Extracts clinical codes** using an Intent Agent (Claude API, optional) with a document-specific extraction protocol, or a regex + keyword-dictionary fallback when no API key is configured.
3. **Scores every Additional Diagnosis** against ACS 0002 (therapeutic treatment / diagnostic investigation / increased clinical care) and assigns a numeric score that drives the code/review/do-not-code decision.
4. **Validates every ICD-10-AM code** through a 4-layer validator (format → MDC existence → PDX acceptability → semantic fallback) before it ever reaches the grouper.
5. **Runs the full AR-DRG V11.0 grouper** (5-step pipeline: demographic edits → Pre-MDC check → MDC assignment → ADRG assignment → complexity split) and returns the DRG, ECCS score, and step-by-step trace.
6. **Flags ambiguous documentation** and generates ACS 0010-compliant clinician queries with multiple-choice response options — the physician answers, and the engine re-codes automatically.
7. **Runs a 5-phase / 12-rule workflow validation** (Dr Kassab's coding competency framework) before allowing submission, covering PDX definition, ADX criteria, COF assignment, external cause completeness, and error-DRG gating.
8. **Gates every claim behind physician approval** — nothing is submitted without an explicit `assert_approved()` pass.

---

## Architecture

```
EHR Documents (PDF/TXT/HL7/FHIR/JSON)
        │
        ▼
┌───────────────────┐
│   Intent Agent     │  Document-specific NLP extraction (Claude API)
│  (or regex + KB)   │  or regex + 140-procedure keyword dictionary
└─────────┬──────────┘
          ▼
┌───────────────────┐
│  ICD-10-AM         │  Layer 1: format · Layer 2: MDC existence
│  Validator         │  Layer 3: PDX acceptability (ACS 0050)
│  (4 layers)        │  Layer 4: Claude API semantic fallback
└─────────┬──────────┘
          ▼
┌───────────────────┐
│ Medical Logic      │  ACS 0002 scoring · ACHI assembly ·
│ Agent              │  Intelligence triggers (exclusion/NCV/hardware/timing)
└─────────┬──────────┘
          ▼
┌───────────────────┐
│ Clinician Query     │  ACS 0010 ambiguity detection → structured
│ Generator            │  physician queries → re-code on response
└─────────┬──────────┘
          ▼
┌───────────────────┐
│  AR-DRG Grouper     │  5-step V11.0 pipeline · MDC lookup (37K codes)
│  (V11.0)            │  ECCS · DCL · Error-DRG gating (960Z/961Z)
└─────────┬──────────┘
          ▼
┌───────────────────┐
│ Workflow Validator  │  5 phases (A–F) · 12 ACS validation rules ·
│ (Critique Agent)    │  Inpatient vs Day-Care logic
└─────────┬──────────┘
          ▼
   Physician Review → Approve/Reject → Submission
```

---

## Repository structure

```
NOVIQ-Clinical-Coding-Intelligence-Platform/
│
├── main.py                          FastAPI backend — all API endpoints
├── noviq_dashboard.html             Single-page review dashboard
├── requirements.txt
├── railway.json
├── sample_episode.json              Test episode (Peritonectomy G13Z)
│
├── engine/
│   ├── noviq_engine.py              Pipeline orchestrator
│   ├── grouper.py                   AR-DRG V11.0 grouper (5-step)
│   ├── models.py                    PatientEpisode / CodingSuggestion schemas
│   ├── validation_rules.py          DCL exclusion + KB-incomplete gating
│   ├── icd_validator.py             4-layer ICD-10-AM validator
│   └── statistical_simulation.py
│
├── knowledge_base/
│   ├── ar_drg_kb_seed_v11_new_adrgs.json    12 ADRGs + 37,157-entry MDC lookup
│   ├── mdc_map.json                          All 25 MDCs, ICD ranges, key ADRGs
│   ├── dcl_exclusions.json                   DCL exclusion principles
│   ├── dcl_table_empirical.json              120 empirical DCL entries
│   ├── keyword_dictionary_medical_logic_v3.json   Intelligence triggers + logic rules
│   ├── keyword_dictionary_medical_logic_v4.json   140 procedures + EHR extraction protocol
│   ├── coding_workflow_kb.json                18-step inpatient/day-care workflow (5 phases, 12 rules)
│   ├── query_templates.json                   20 ACS 0010 clinician query templates
│   └── icd10am_validator_kb.json               PDX acceptability rules (ACS 0050)
│
├── docs/
│   └── grouper_pseudocode.md
│
└── tests/
    ├── test_grouper.py
    └── test_pipeline.py
```

---

## Current status

| Component | Status |
|---|---|
| AR-DRG V11.0 grouper (5-step) | ✅ Implemented, test suite passing |
| MDC map (all 25 MDCs) | ✅ 37,157 PDX codes, clinically validated |
| ICD-10-AM 4-layer validator | ✅ Implemented, wired into `/api/process` |
| ACS 0002 per-diagnosis scoring | ✅ Implemented (Intent Agent + regex fallback) |
| Clinician query generator (ACS 0010) | ✅ 20 templates, 23 triggers, respond-and-recode loop |
| Workflow validation (5 phases / 12 rules) | ✅ Implemented, surfaced in dashboard |
| ADRG coverage | 🟡 12 ADRGs seeded — extending to broader case coverage |
| AR-DRG V12.0 compatibility | 🔴 Not yet implemented — **V12.0 is the live version in Australian hospitals as of 1 July 2026** |
| F25 ECCS threshold | 🔴 Blocked — requires AR-DRG Definitions Manual (Lane Print, commercial) |
| Full DCL lookup table | 🔴 Blocked — same commercial source |
| Complete Appendix C exclusion lists | 🔴 Blocked — same commercial source |
| FHIR R4 / HL7 v2 certified connectors | 🟡 Basic parsers only |

---

## Running locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` for the dashboard, or `http://localhost:8000/docs` for the FastAPI auto-generated API reference.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | No | Enables the Intent Agent (NLP extraction) and Layer 4 semantic ICD validation. Without it, the engine falls back to regex + keyword-dictionary extraction — fully functional, lower extraction accuracy on free-text documents. |

### API endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/upload` | Upload EHR files → parsed episode + clinician queries |
| `POST` | `/api/process/{episode_id}` | Run full pipeline → coding suggestion + workflow report + ICD validation |
| `POST` | `/api/query/{episode_id}/respond` | Submit physician responses to clinician queries → re-code |
| `POST` | `/api/approve/{episode_id}` | Physician approval/rejection gate |
| `GET` | `/api/queue` | All episodes with status |
| `GET` | `/api/episode/{episode_id}` | Full episode detail |
| `GET` | `/api/kb/status` | Knowledge base health check |
| `GET` | `/api/health` | Full diagnostic — engine, KB, and Intent Agent status |

---

## Deployment

Currently deployable to any platform that supports a Python/ASGI service connected to a GitHub repository (Render, Fly.io, Koyeb, or self-hosted via Docker). Push to `main` and connect the repo — no Dockerfile required; the platform's Python buildpack picks up `requirements.txt` and runs `uvicorn main:app`.

---

## Standards & scope

- **ICD-10-AM** 11th Edition
- **ACHI** 12th Edition
- **AR-DRG** V11.0 (V12.0 compatibility layer in progress)
- **ACS** standards referenced throughout: 0001, 0002, 0010, 0016, 0017, 0019, 0031, 0048, 0050, 2001–2005

## License

MIT — see `pyproject.toml`.
