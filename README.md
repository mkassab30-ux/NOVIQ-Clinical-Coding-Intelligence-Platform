# NOVIQ Engine — Clinical Coding Intelligence Platform

**AR-DRG V11.0 · ICD-10-AM Twelfth Edition · FHIR R4 Compatible · Australian Healthcare**

> *"Clinical Truth Preservation Engine — protecting against upcoding and revenue leakage simultaneously."*

NOVIQ Engine is an AI-powered clinical coding intelligence platform built for the Australian healthcare system. It reads a patient's complete EHR before any claim is submitted and produces accurate, ethically justified AR-DRG codes — with built-in protection against both upcoding and revenue leakage — before physician approval and claim submission.

**Founder:** Dr. Mohamed Kassab — General Surgeon · Healthcare Operations · Insurance/TPA

---

## Build status

| Phase | Description | Tests | Status |
|-------|-------------|-------|--------|
| 0 | ACS Scoring Engine · Knowledge Base scaffold · Keyword Dictionary | — | ✅ Complete |
| 1 | AR-DRG V11.0 KB seed (B08, F25, G13) · ACHI trigger codes · Split profiles | — | ✅ Complete |
| 2 | DCL Exclusion module · Validation rules · ECCS utilities · Statistical simulation | — | ✅ Complete |
| 3 | Grouper pseudocode · AR-DRG V11.0 grouper · Test suite | 18/18 ✅ | ✅ Complete |
| 4 | Data models · Pipeline orchestrator · Physician approval gate · Integration tests | 45/45 ✅ | ✅ Complete |
| 5 | Intelligence Layer · Intent Agent · Medical Logic Agent · Keyword expansion | — | 🔄 Next |
| 6 | FHIR R4 adapter · NPHIES/UHI output · HL7 v2 connector | — | Planned |
| 7 | MVP hospital pilot | — | Planned |

**Total: 4,804 lines · 63 tests · All green**

---

## Architecture

```
EHR Documents (FHIR R4 / HL7 v2)
         │
         ▼
┌──────────────────────────────────────────────────────┐
│                    NOVIQ Engine                      │
│                                                      │
│  ┌──────────────┐      ┌───────────────────────┐     │
│  │ Intent Agent │      │ Medical Logic Agent   │     │
│  │  (Phase 5)   │─────▶│  (Phase 5)            │     │
│  └──────────────┘      └───────────┬───────────┘     │
│                                    ▼                 │
│  ┌─────────────────────────────────────────────┐     │
│  │          AR-DRG V11.0 Engine (Core)         │     │
│  │                                             │     │
│  │  ACS Scoring ──▶ Validation ──▶ Grouper     │     │
│  │  (5-step pipeline · JSON-In/JSON-Out)        │     │
│  └──────────────────────┬──────────────────────┘     │
│                         ▼                            │
│  ┌─────────────────────────────────────────────┐     │
│  │      Critique & Ethics Agent (Phase 5)      │     │
│  │  Upcoding risk · Revenue leakage · ACS      │     │
│  └──────────────────────┬──────────────────────┘     │
└────────────────────────────────────────────────────  ┘
                          │
                          ▼
                Physician Approval Gate
                (non-negotiable — assert_approved()
                 must pass before claim submission)
                          │
                          ▼
                NPHIES / UHI / Payer
```

### Design principles

- **JSON-In / JSON-Out** — every module accepts a `PatientEpisode` dict and returns a typed JSON result. Zero UI coupling. Zero DB calls inside modules.
- **FastAPI-ready** — any module wraps in 3 lines: `@app.post("/process") async def process(ep: dict) -> dict: return engine.process_episode_dict(ep)`
- **Physician approval gate** — `CodingSuggestion.assert_approved()` is a hard gate. No claim exits without `approved_by` set.
- **Dual protection** — flags both upcoding risk (excluded codes inflating ECCS) and revenue leakage (undercoded procedures) simultaneously.
- **ACS score is the source of truth** — coding decisions are anchored to ACS 0001/0002 scores, not the Discharge Summary.
- **Version-aware** — immutable KB per AR-DRG version. V12.0 (July 2026) is a config swap, not a rewrite.

---

## Repository structure

```
NOVIQ-Clinical-Coding-Intelligence-Platform/
│
├── engine/
│   ├── noviq_engine.py              # Pipeline orchestrator — primary entry point
│   ├── grouper.py                   # AR-DRG V11.0 grouper (5-step pipeline)
│   ├── validation_rules.py          # DCL exclusion module + ECCS utilities
│   ├── models.py                    # EpisodeRecord · ACSScore · CodingSuggestion
│   └── statistical_simulation.py   # RID · L3H3 trimming · threshold simulation
│
├── knowledge_base/
│   ├── ar_drg_kb_seed_v11_new_adrgs.json   # B08, F25, G13 + global V11.0 flags
│   ├── dcl_exclusions.json                  # Appendix C exclusion KB (scaffold)
│   └── keyword_dictionary_v11_new_adrgs.json # ACHI trigger + modifier codes
│
├── docs/
│   └── GROUPER_PSEUDOCODE.md         # Approved grouper pseudocode (Phase 3)
│
├── tests/
│   ├── test_grouper.py               # 18-assertion grouper test suite
│   └── test_pipeline.py              # 45-assertion end-to-end integration tests
│
└── README.md
```

---

## Quick start

```python
from engine.noviq_engine import NOVIQEngine

engine = NOVIQEngine()

episode = {
    "episode_id":      "EP-2026-001",
    "patient_age":     58,
    "patient_sex":     "Female",
    "pdx":             "C48.1",
    "adx":             ["E11.9", "E61.1"],
    "achi_codes":      ["96211-00"],
    "los_days":        12,
    "same_day":        False,
    "separation_mode": "discharge_home",
    "care_type":       "01",
    "acs_pdx_score":   6,
    "ehr_documents":   ["Operation Notes", "Discharge Summary"],
}

suggestion = engine.process_episode(episode)

print(suggestion.ar_drg_code)           # → "G13Z"
print(suggestion.approval_status)       # → "PENDING"
print(suggestion.upcoding_risk_count)   # → 1  (E61.1 flagged)
print(suggestion.flags[0])              # → "UPCODING RISK: E61.1 ..."

# Physician gate — required before submission
suggestion.approve("DR-KASSAB-001")
suggestion.assert_approved()            # passes
claim = suggestion.to_dict()            # FHIR-compatible output
```

---

## Core modules

### `engine/noviq_engine.py`

Single callable for the full pipeline. All dependencies injected and swappable.

```python
NOVIQEngine.process_episode(episode_dict)     → CodingSuggestion
NOVIQEngine.process_episode_dict(episode_dict) → dict  # for FastAPI
```

### `engine/grouper.py`

AR-DRG V11.0 5-step pipeline. Confirmed from IHACPA Final Report + Technical Specifications.

| Step | Logic | V11.0 change |
|------|-------|--------------|
| 1 | Demographic & clinical edits | Sex conflict → FLAG only (test removed) |
| 2 | Pre-MDC override check | B08, F25, G13 confirmed NOT Pre-MDC |
| 3 | MDC assignment via PDX | `R10.2` only remaining sex-routing PDX |
| 4 | ADRG via intervention hierarchy | B08=pos2/MDC01 · F25=pos13/MDC05 · G13=pos1/MDC06 |
| 5 | DRG via ECCS | Exclusions → DCL → ECCS(0.86) → threshold → suffix |

### `engine/validation_rules.py`

```python
validate_episode(episode_dict)           → ValidationResult
compute_eccs(dcl_values)                 → float
compute_eccs_with_trace(dcl_values)      → dict  # step-by-step audit trail
```

ECCS formula — AR-DRG V11.0 Technical Specifications §4.5:
```
ECCS(e) = Σ [ DCL(xᵢ, A) × (0.86)^(i-1) ]   DCLs sorted descending
Verified: [4,3,2,1,0] → 8.6953 ✓
```

### `engine/models.py`

```python
EpisodeRecord      # from_dict() · to_dict() · to_grouper_input()
ACSScore           # ACS 0001/0002 per-diagnosis result
CodingSuggestion   # approve() · reject() · assert_approved() · to_dict()
```

---

## Knowledge Base — V11.0 new ADRGs

| ADRG | Description | MDC | Split | Hierarchy | ECCS threshold |
|------|-------------|-----|-------|-----------|----------------|
| B08 | Endovascular Clot Retrieval | 01 | A/B | pos 2 | ≥ 3.0 ✓ |
| F25 | Percutaneous Heart Valve Replacement with Bioprosthesis | 05 | A/B | pos 13 | **null ⚠** |
| G13 | Peritonectomy for Gastrointestinal Disorders | 06 | Z | pos 1 | N/A ✓ |

**F25 production gate** — `KnowledgeBaseIncompleteError` raised until threshold populated from Definitions Manual. All other ADRGs operational.

---

## Tests

```
tests/test_grouper.py      18/18 PASS  — grouper unit tests
tests/test_pipeline.py     45/45 PASS  — end-to-end integration
─────────────────────────────────────
Total                      63/63 PASS
```

Scenarios covered: G13Z peritonectomy · B08 ECR hierarchy · 960Z/961Z/963Z error DRGs · R10.2 sex-routing · F25 production gate · ECCS formula · DCL exclusion (upcoding risk) · Physician gate (approve/reject/block) · FHIR output · ACS threshold routing · EpisodeRecord round-trip

---

## Roadmap

### Milestone 1 — Core Engine ✅ COMPLETE (Phases 0–4)
AR-DRG V11.0 grouper · DCL validation · ACS scoring · Physician gate · 63 passing tests

### Milestone 2 — Intelligence Layer (Phase 5)
Keyword Dictionary expansion (20+ procedures) · Intent Agent · Medical Logic Agent · Critique & Ethics Agent

### Milestone 3 — FHIR Integration + NPHIES adapter (Phase 6)
FHIR R4 input adapter · HL7 v2 connector · NPHIES/UHI output · "Ready to Plug" for any Australian HMIS

### Milestone 4 — MVP hospital pilot (Phase 7)
50-episode anonymised pilot · Physician review UI · Revenue impact report · Performance benchmarking

---

## Open blockers (Definitions Manual — purchase required)

| Item | Status | Path |
|------|--------|------|
| F25 ECCS threshold | null — production gate active | Lane Print: ar-drg.laneprint.com.au |
| Full DCL lookup table | Stub (ECCS=0) | Licensed grouper software |
| Appendix C Table C1+C2 | 7/47 confirmed | Definitions Manual Volume 3 |

---

## V12.0 readiness

AR-DRG V12.0 proposed live: **1 July 2026**. Architecture is version-aware — upgrade = new KB file, not a code rewrite.

---

## Source authority

| Document | Access |
|----------|--------|
| AR-DRG V11.0 Final Report (January 2023) | Free — ihacpa.gov.au |
| AR-DRG V11.0 Technical Specifications | Free — ihacpa.gov.au |
| AR-DRG V11.0 Definitions Manual (Volumes 1–3) | Purchase — ar-drg.laneprint.com.au |
| ICD-10-AM/ACHI/ACS Twelfth Edition | Purchase — ihacpa.gov.au |

---

## License

Private repository. All rights reserved. © Dr. Mohamed Kassab / Noviq Health 2026.
