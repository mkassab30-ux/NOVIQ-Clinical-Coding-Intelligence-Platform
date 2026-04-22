# NOVIQ Engine — Clinical Coding Intelligence Platform

> **AR-DRG V11.0 · ICD-10-AM 12th Edition · Live on Railway**
>
> Built by **Dr. Mohamed Kassab** — General Surgeon · Healthcare Operations · Insurance/TPA
> Founder, **Noviq Health**

[![Live Demo](https://img.shields.io/badge/Live%20Demo-Railway-6366f1?style=flat-square)](https://noviq-clinical-coding-intelligence-platform-production-91be.up.railway.app/)
[![AR-DRG](https://img.shields.io/badge/AR--DRG-V11.0-4ECDC4?style=flat-square)](https://www.ihacpa.gov.au)
[![ICD-10-AM](https://img.shields.io/badge/ICD--10--AM-12th%20Edition-FFE66D?style=flat-square)](https://www.ihacpa.gov.au)
[![Tests](https://img.shields.io/badge/Tests-63%20passing-69DB7C?style=flat-square)](#testing)
[![Python](https://img.shields.io/badge/Python-3.12-blue?style=flat-square)](https://python.org)

---

## What is NOVIQ Engine?

NOVIQ Engine is an AI-powered clinical coding intelligence platform purpose-built for the **Australian healthcare system**. It reads patient EHR documents before claim submission and produces accurate **ICD-10-AM**, **ACHI**, and **AR-DRG** codes — with explicit protection against **both upcoding and revenue leakage simultaneously**.

The core design principle: **Clinical Truth Preservation**. The engine never upcodes, never undercodes, and requires **physician approval** before any claim can exit the system.

```
EHR Documents → Intent Agent → ACS Scoring → AR-DRG Grouper → Critique Agent → Physician Gate → Claim
```

---

## Live System

| Component | Status |
|-----------|--------|
| **Production URL** | https://noviq-clinical-coding-intelligence-platform-production-91be.up.railway.app/ |
| **Platform** | Railway (auto-deploy from GitHub) |
| **Engine Mode** | Live (AR-DRG V11.0 Grouper active) |
| **Dashboard** | 4-case simultaneous review |

---

## Architecture

```
NOVIQ-Clinical-Coding-Intelligence-Platform/
├── engine/                          ← Core AR-DRG V11.0 engine (5 modules)
│   ├── noviq_engine.py              ← Pipeline orchestrator — primary entry point
│   ├── grouper.py                   ← AR-DRG V11.0 5-step grouper
│   ├── models.py                    ← EpisodeRecord, ACSScore, CodingSuggestion
│   ├── validation_rules.py          ← DCL exclusions, ECCS utilities, upcoding detection
│   └── statistical_simulation.py   ← ECCS decay model, GLM framework
├── knowledge_base/
│   ├── ar_drg_kb_seed_v11_new_adrgs.json   ← B08, F25, G13 + MDC lookup (52 entries)
│   ├── dcl_exclusions.json                  ← Appendix C unconditional + conditional
│   ├── keyword_dictionary_medical_logic_v3.json  ← 152 procedures, 6 specialties, 17 triggers
│   └── keyword_dictionary_v11_new_adrgs.json
├── tests/
│   ├── test_grouper.py              ← 18 grouper tests
│   └── test_pipeline.py             ← 45 pipeline tests
├── docs/
│   └── GROUPER_PSEUDOCODE.md        ← Approved 12-decision grouper design
├── main.py                          ← FastAPI backend (7 endpoints)
├── noviq_dashboard.html             ← Dashboard UI
├── sample_episode.json              ← Test episode (G13Z expected)
└── requirements.txt
```

---

## Core Engine — 5 Python Modules

### `noviq_engine.py` — Pipeline Orchestrator
Single entry point. Runs a `PatientEpisode` through all modules in sequence.
```python
engine = NOVIQEngine()
suggestion = engine.process_episode(episode_dict)
suggestion.approve("DR-KASSAB-001")
suggestion.assert_approved()          # Hard gate — no claim exits without this
claim = suggestion.to_dict()
```

### `grouper.py` — AR-DRG V11.0 Grouper
Implements the 5-step official grouper pipeline:
1. **Demographic & clinical edits** → error DRGs 960Z / 961Z / 963Z
2. **Pre-MDC override** — check for B08/F25/G13 triggers
3. **MDC assignment** — PDX → MDC via 52-entry lookup (C48.1 → MDC 06)
4. **ADRG assignment** — ACHI trigger code matching within MDC hierarchy
5. **Final DRG via ECCS** — DCL exclusions → decay formula → threshold comparison

### `validation_rules.py` — DCL & ECCS Engine
- Appendix C unconditional exclusions (E61.1, Z55–Z65 ranges, etc.)
- Appendix C conditional exclusions (R06.0 when J44.9 present, etc.)
- **ECCS formula**: `ECCS(e) = Σ[ DCL(xᵢ, A) × (0.86)^(i-1) ]`
- Upcoding risk detection

### `models.py` — Data Models
`EpisodeRecord` → `ACSScore` → `CodingSuggestion` with full provenance chain.

### `statistical_simulation.py` — Statistical Framework
Gamma GLM with log-link, L3H3 outlier trimming, decay factor 0.86 (confirmed Section 4.5, AR-DRG V11.0 Technical Specifications).

---

## Knowledge Base

### AR-DRG KB (`ar_drg_kb_seed_v11_new_adrgs.json`)

Three new V11.0 ADRGs — fully seeded:

| ADRG | Description | Split | Trigger Code | Status |
|------|-------------|-------|-------------|--------|
| **B08** | Endovascular Clot Retrieval | A/B (threshold ≥ 3.0) | 35414-00 | ✅ Live |
| **F25** | Percutaneous Heart Valve Replacement | A/B (threshold null) | 38488-08/09/10/11 | ⛔ Blocked |
| **G13** | Peritonectomy for GI Disorders | Z (unsplit) | 96211-00 | ✅ Live |

MDC PDX Lookup: **52 entries** covering MDC 01, 05, 06, 07, 08, 09, 11, 12, 13, 14, 17, 23.

### Medical Logic KB (`keyword_dictionary_medical_logic_v3.json`)

| Specialty | Procedures | Key Logic Rules |
|-----------|-----------|-----------------|
| General Surgery | 41 | Skin lesion 6S Rule, Thyroid RLN gate, Appendectomy pediatric 12h rule, Hemorrhoid Grade 3/4 gate, Whipple tier |
| Hand Surgery | 38 | NCV gate for CTS, Tendon timing rule, Mode of Injury exclusion, Hardware auditor |
| Bariatric | 22 | BMI > 40 gate (auto-calculate), Methylene Blue checker, Revisional protocol |
| Breast Surgery | 15 | Reduction spinal ICD gate, Oncology safety margin, Emergency auto-approve |
| Plastic & Reconstructive | 14 | Abdominoplasty hernia gate, Scar revision C-section only, Free flap anastomosis |
| Orthopaedic | 22 | IM nail vs plate rule, Hip age gate, Knee MRI gate, PRP auto-reject, Scoliosis congenital exclude |
| **TOTAL** | **152** | **17 intelligence triggers** |

### 17 Intelligence Triggers

| Trigger | Action |
|---------|--------|
| `exclusion_hunter` | AUTO_REJECT if fighting/self-harm/hazardous sport in ER notes |
| `bmi_calculator` | REJECT bariatric if BMI ≤ 40 (auto-calculated) |
| `cosmetic_filter` | AUTO_REJECT Augmentation/Liposuction without medical indication |
| `mri_gate` | FLAG_MISSING_IMAGING for Knee arthroscopy/Spinal fusion |
| `age_gate` | FLAG DHS vs THR decision for elderly hip fracture |
| `prp_rejector` | AUTO_REJECT PRP injections (not FDA approved) |
| `methylene_blue_checker` | FLAG if absent from Bariatric Leak Repair notes |
| `ncv_matcher` | FLAG_REJECTION_RISK for CTS without NCV/EMG |
| `oncologic_margin_checker` | FLAG if Safety Margin or LN Excision absent |
| `emergency_auto_approve` | Bypass pre-auth for emergency procedures |
| `hardware_auditor` | FLAG Plate & Screws on non-comminuted fracture |
| `robotic_rejector` | AUTO_DOWNGRADE Robotic Appendectomy |
| `rln_safety_alert` | ALERT if RLN/Parathyroid absent from Total TT notes |
| `senior_review_flag` | FLAG complex/misdiagnosis-prone cases |
| `specimen_matcher` | FLAG lab container count mismatch |
| `occupational_alert` | FLAG Trigger Finger in manual labor for Worker's Comp |
| `timing_checker` | UPGRADE_TIER Tendon repair > 24h post-injury |

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/` | Dashboard UI |
| `POST` | `/api/upload` | Upload EHR files → parse → return PatientEpisode |
| `POST` | `/api/process/{id}` | Run full pipeline → CodingSuggestion |
| `POST` | `/api/approve/{id}` | Physician approve/reject |
| `GET`  | `/api/queue` | All episodes with status |
| `GET`  | `/api/episode/{id}` | Full episode detail |
| `GET`  | `/api/kb/search?q=cholecystectomy` | Search Medical Logic KB |
| `GET`  | `/api/kb/status` | KB health + engine mode |
| `GET`  | `/api/health` | System diagnostic |

### Supported File Formats for Upload

| Format | What is extracted |
|--------|------------------|
| `.json` | Full EpisodeRecord (pre-structured) |
| `.txt / .text` | Patient name, age, sex, ICD codes, ACHI codes, LOS |
| `.pdf` | pdfplumber text extraction → same as .txt |
| `.hl7` | PID, DG1, PR1 segments |
| `.xml` | FHIR R4 Encounter/Condition/Procedure |

---

## Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
python -m pytest tests/ -v

# Expected: 63/63 passing
# test_grouper.py:   18 tests
# test_pipeline.py:  45 tests
```

### Key test cases verified:
- `C48.1` + `96211-00` → **G13Z** ✅
- `I63.3` + `35414-00` → **B08B** ✅
- `I35.0` + `38488-08` → **KnowledgeBaseIncompleteError** ✅ (F25 production gate)
- `E61.1` → **excluded from ECCS** ✅ (Appendix C)
- `Z59.0` → **excluded from ECCS** ✅ (socioeconomic)
- Physician gate: `assert_approved()` raises `PermissionError` before approval ✅

---

## Run Locally

```bash
# 1. Clone
git clone https://github.com/mkassab30-ux/NOVIQ-Clinical-Coding-Intelligence-Platform.git
cd NOVIQ-Clinical-Coding-Intelligence-Platform

# 2. Install
pip install -r requirements.txt

# 3. Start
python -m uvicorn main:app --reload --port 8000

# 4. Open
# http://localhost:8000

# 5. Test — upload sample_episode.json → expect G13Z
```

**Expected startup output:**
```
[OK] Engine loaded from: .../engine
[OK] Medical Logic KB v3.0.0 — 152 procedures | 17 triggers
[OK] NOVIQEngine initialised
[OK] NOVIQ v3 ready — LIVE | KB=.../knowledge_base
```

---

## Open Blockers (Require Purchase)

Three items require the **AR-DRG V11.0 Definitions Manual** (Lane Print: ar-drg.laneprint.com.au):

| Blocker | Impact | Workaround |
|---------|--------|-----------|
| **F25 ECCS threshold** (null) | F25A/F25B cannot be assigned | `KnowledgeBaseIncompleteError` raised — hard production gate |
| **Full DCL lookup table** (~6.8M pairs) | ECCS = 0.0 for all episodes | Empirical DCL table planned (see roadmap) |
| **Appendix C Table C1+C2** (complete) | Only 7/47 unconditional exclusions confirmed | Partial exclusion checking active |

> **ECCS Score note:** Until the full DCL table is licensed, ECCS will report 0.0. The DRG suffix (A/B/C/Z) is correctly assigned for G13 (unsplit = Z regardless) and B08 (defaults to B08B = minor complexity). F25 is hard-blocked until threshold is purchased.

---

## Roadmap

### ✅ Phase 1–4 — Core Engine (Complete)
- AR-DRG V11.0 grouper (5-step, 63 tests)
- ACS Scoring Engine (PDX 7pts, ADX 8pts)
- Physician approval gate (`assert_approved()`)
- FastAPI backend (7 endpoints)
- Railway deployment

### ✅ Phase 5 — Intelligence Layer (Partial)
- Medical Logic KB v3 (6 specialties, 152 procedures)
- 17 Intelligence Triggers
- **Pending:** ENT, Urology, Cardiology, Cardiothoracic, Neurology (4 specialties)
- **Pending:** Medical Abbreviations KB
- **Pending:** Internal Medicine clinical guide

### 🔄 Phase 5b — Empirical DCL Table (Next)
Build `dcl_table_empirical.json` with literature-derived DCL values for the 200 most common diagnoses. This enables **real ECCS scores** without the Definitions Manual. Based on:
- Published AR-DRG V10/V11 research
- AIHW cost weight data (public)
- Clinical complexity literature

### 🔜 Phase 6 — FHIR Integration
- FHIR R4 adapter: `Encounter` resource → `EpisodeRecord`
- HL7 v2 connector (PID/DG1/PR1 full parser)
- NPHIES/UHI output format (Saudi Arabia)
- Target: connect to any hospital HMIS without manual JSON upload

### 🔜 Phase 7 — AR-DRG V12.0 Compatibility
- V12.0 proposed live date: **1 July 2026**
- Version-aware architecture required in `grouper.py`
- New ADRGs to be seeded when Final Report published

### 🔜 Phase 8 — Hospital Pilot
- Replace in-memory store → PostgreSQL
- Multi-physician role management
- Audit trail export
- HIMSS integration

---

## ECCS Score — Transparent Explanation for MVP

The **ECCS (Episode Clinical Complexity Score)** measures how much additional resource use the comorbidities add to the principal diagnosis.

```
ECCS = DCL₁ + (0.86 × DCL₂) + (0.86² × DCL₃) + ...
```

- **DCL** = Diagnosis Complexity Level (0–5 per diagnosis, ADRG-specific)
- **0.86** = global decay factor (confirmed AR-DRG V11.0 Technical Specifications Section 4.5)
- DCLs are sorted **descending** before applying decay
- Codes excluded by Appendix C receive DCL = 0

**Current MVP status:** DCL table = stub (all DCLs = 0 → ECCS = 0.0). DRG suffix correctly assigned for unsplit ADRGs (G13Z). Empirical DCL table planned for Phase 5b.

**ACS Score** (operational now):
- PDX ≥ 5/7 → code as principal diagnosis ✅
- ADX ≥ 5/8 → code as additional diagnosis ✅
- ADX 3–4/8 → physician review ✅
- ADX < 3/8 → do not code ✅

---

## Clinical Standards Reference

| Standard | Version | Source |
|----------|---------|--------|
| AR-DRG Classification | V11.0 | IHACPA (effective 2023-07-01) |
| ICD-10-AM / ACHI / ACS | 12th Edition | IHACPA |
| ECCS decay factor | 0.86 | AR-DRG V11.0 Technical Specifications, Section 4.5 |
| ACS 0001 (PDX scoring) | 12th Ed | ICD-10-AM/ACHI/ACS Twelfth Edition |
| ACS 0002 (ADX scoring) | 12th Ed | ICD-10-AM/ACHI/ACS Twelfth Edition |
| Appendix C exclusions | V11.0 | AR-DRG V11.0 Definitions Manual (partial — purchase required) |

---

## About

**Dr. Mohamed Kassab** — General Surgeon, Healthcare Operations & Insurance/TPA specialist.
Founder, **Noviq Health** — building the next generation of clinical coding intelligence for the Australian and Middle Eastern healthcare markets.

> *"The goal is not automation of coding. The goal is Clinical Truth Preservation — ensuring every claim reflects exactly what happened clinically, nothing more, nothing less."*

---

*NOVIQ Engine — AR-DRG V11.0 · 4,800+ lines · 63 tests · All green*
*Built with Claude (Anthropic) — Phase 1 through Phase 5 · April 2026*
