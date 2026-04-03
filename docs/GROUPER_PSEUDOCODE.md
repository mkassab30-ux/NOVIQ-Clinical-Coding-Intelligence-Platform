# NOVIQ Engine — AR-DRG V11.0 Grouper Pseudocode
**Phase 3, Deliverable 3.1**
**Status: FOR REVIEW — sign off before Python implementation**

Source authority: AR-DRG V11.0 Final Report + Technical Specifications, IHACPA
Chain of Truth: Data prep → DCL → Exclusions → ECCS → Threshold → Final DRG
Runtime steps implemented here: Step 4 (Exclusions), Step 5 (ECCS), Step 6 (Threshold)
Development-time only (IHACPA): Steps 1–3 (trimming, cost model, DCL derivation)

---

## Architecture contract

```
EpisodeRecord (JSON-In)
        │
        ▼
┌───────────────────────────────────────────────┐
│               ARDRGGrouper                    │
│                                               │
│  STEP 1 ── Demographic & Clinical Edits       │
│       │    Error DRGs: 960Z / 961Z / 963Z     │
│       ▼                                       │
│  STEP 2 ── Pre-MDC Override Check             │
│       │    Bypasses MDC if triggered          │
│       ▼                                       │
│  STEP 3 ── MDC Assignment via PDX             │
│       │    R10.2 sex-routing edge case        │
│       ▼                                       │
│  STEP 4 ── ADRG Assignment via Hierarchy      │
│       │    First ACHI trigger match wins      │
│       ▼                                       │
│  STEP 5 ── DRG Assignment via ECCS            │
│            Exclusions → DCL lookup →          │
│            ECCS (0.86 decay) → threshold      │
└───────────────────────────────────────────────┘
        │
        ▼
GrouperResult (JSON-Out, FHIR-compatible)
```

---

## Input — EpisodeRecord

```
EpisodeRecord {
    episode_id        : string
    patient_age       : integer          -- years at admission
    patient_sex       : "Male" | "Female" | "Other" | "Unknown"
    admission_weight  : integer | null   -- grams, neonates only
    same_day          : boolean
    separation_mode   : string
    los_days          : integer
    pdx               : string           -- principal ICD-10-AM code
    adx               : list[string]     -- additional ICD-10-AM codes
    achi_codes        : list[string]     -- ACHI intervention codes
    hours_mech_vent   : integer | null
    care_type         : string           -- 01=Acute 07=Newborn 11=MentalHealth
}
```

---

## Output — GrouperResult (FHIR-compatible)

```
GrouperResult {
    episode_id         : string
    ar_drg_version     : "V11.0"
    ar_drg_code        : string          -- final DRG e.g. "B08A", "G13Z"
    ar_drg_description : string
    adrg_code          : string          -- e.g. "B08"
    mdc                : string | null   -- e.g. "01" (null for Pre-MDC)
    partition          : "intervention" | "medical" | "pre_mdc" | "error"
    grouping_status    : "SUCCESS" | "ERROR" | "WARNING"
    eccs               : float
    dcl_contributions  : list[DCLEntry]
    threshold_used     : float | null
    edit_flags         : list[string]    -- non-blocking warnings
    error_code         : string | null
    errata_applied     : list[string]    -- e.g. ["Errata1_2023-04-01"]
    step_trace         : list[string]    -- full audit trail
    grouped_at         : ISO8601 UTC
}

DCLEntry {
    diagnosis_code   : string
    dcl_value        : integer 0-5
    is_principal     : boolean
    is_excluded      : boolean
    exclusion_type   : "unconditional" | "conditional" | "socioeconomic" | null
    exclusion_reason : string | null
}
```

---

## STEP 1 — Demographic & Clinical Edits

**Purpose:** Validate all inputs before any grouping logic runs.
Terminal failures assign an error DRG and exit immediately.

**V11.0 locked decisions:**
- Sex conflict TEST removed (V11.0 change) — warning FLAG only, non-blocking
- Invalid ADX/ACHI codes stripped silently — episode continues grouping
- All three error DRGs exit only from this step

```
FUNCTION step_1_edits(episode) → Step1Result:

    flags = []

    // 1a. patient_sex must be valid
    IF episode.patient_sex NOT IN ["Male","Female","Other","Unknown"]:
        RETURN error_exit("960Z", "Invalid sex: " + episode.patient_sex)

    // 1b. PDX must be present
    IF episode.pdx IS NULL OR EMPTY:
        RETURN error_exit("960Z", "No principal diagnosis provided")

    // 1c. PDX must be acceptable as a principal diagnosis code
    IF NOT is_valid_pdx(episode.pdx):
        RETURN error_exit("961Z", "Unacceptable PDX: " + episode.pdx)

    // 1d. Neonatal consistency
    IF is_neonatal_pdx(episode.pdx):
        IF episode.patient_age > 0:
            RETURN error_exit("963Z",
                "Neonatal PDX " + episode.pdx +
                " inconsistent with age " + str(episode.patient_age))
        IF episode.admission_weight IS NOT NULL:
            IF NOT weight_consistent(episode.pdx, episode.admission_weight):
                RETURN error_exit("963Z",
                    "Neonatal PDX inconsistent with weight " +
                    str(episode.admission_weight))

    // 1e. Strip invalid ADX codes (non-fatal)
    episode.adx = [c FOR c IN episode.adx IF is_valid_icd_code(c)
                   ELSE flags.APPEND("ADX stripped: " + c)]

    // 1f. Strip invalid ACHI codes (non-fatal)
    episode.achi_codes = [c FOR c IN episode.achi_codes
                          IF is_valid_achi_code(c)
                          ELSE flags.APPEND("ACHI stripped: " + c)]

    // 1g. Sex conflict FLAG — warning only (V11.0: test removed, flag retained)
    FOR code IN (episode.adx + episode.achi_codes):
        IF sex_conflicts_with_code(episode.patient_sex, code):
            flags.APPEND("SEX_CONFLICT_WARN: " + code +
                         " [warning only — does not affect grouping in V11.0]")

    // 1h. Age conflict FLAG — warning only
    FOR code IN ([episode.pdx] + episode.adx):
        IF age_conflicts_with_code(episode.patient_age, code):
            flags.APPEND("AGE_CONFLICT_WARN: " + code)

    RETURN Step1Result(status="PASS", edit_flags=flags, episode=episode)
```

---

## STEP 2 — Pre-MDC Override Check

**Purpose:** Identify very high-cost episodes triggered by a specific
intervention that overrides MDC assignment entirely.

**V11.0 locked decisions:**
- B08, F25, G13 are NOT Pre-MDC (confirmed Final Report)
- If triggered: Step 3 is skipped entirely, go directly to Step 5

```
FUNCTION step_2_pre_mdc(episode) → Step2Result:

    FOR adrg IN PRE_MDC_ADRG_LIST (ordered by hierarchy_position ASC):
        FOR trigger IN adrg.trigger_codes:
            IF trigger IN episode.achi_codes:
                RETURN Step2Result(
                    triggered    = True,
                    matched_adrg = adrg.adrg_code,
                    trace        = "Step 2: Pre-MDC trigger " + trigger +
                                   " → " + adrg.adrg_code +
                                   " — MDC assignment bypassed"
                )

    RETURN Step2Result(
        triggered = False,
        trace     = "Step 2: No Pre-MDC trigger → proceeding to MDC"
    )
```

---

## STEP 3 — MDC Assignment via PDX

**Purpose:** Assign MDC from principal diagnosis.

**V11.0 locked decisions:**
- R10.2 is the ONLY PDX still requiring sex as a routing variable
- All other sex-dependent routing resolved in ICD-10-AM Twelfth Edition

```
FUNCTION step_3_mdc(episode) → Step3Result:

    pdx = episode.pdx

    // SPECIAL CASE: R10.2 — only remaining sex-routing PDX in V11.0
    IF pdx == "R10.2":
        IF episode.patient_sex IN ["Male", "Other", "Unknown"]:
            mdc = "12"  // Male Reproductive System
        ELSE:
            mdc = "13"  // Female Reproductive System
        RETURN Step3Result(mdc=mdc,
            trace="Step 3: R10.2 sex-routing → " +
                  episode.patient_sex + " → MDC " + mdc)

    // Standard lookup
    mdc = MDC_PDX_LOOKUP.get(pdx)

    IF mdc IS NULL:
        RETURN error_exit("960Z", "PDX " + pdx + " has no MDC mapping")

    RETURN Step3Result(mdc=mdc,
        trace="Step 3: " + pdx + " → MDC " + mdc)

// MDC_PDX_LOOKUP source: Definitions Manual Appendix A (purchased)
```

---

## STEP 4 — ADRG Assignment via Intervention Hierarchy

**Purpose:** Walk intervention hierarchy for the MDC. First ACHI match wins.
Fall back to medical partition if no intervention match.

**V11.0 locked decisions:**
- Hierarchy is strictly positional — no clinical weighting at runtime
- Modifier codes (G13 HIPEC) do NOT trigger ADRG — trigger codes only
- ADRG 801 is the final fallback

**Hierarchy positions confirmed (V11.0 Final Report Table 3):**
- MDC 01: B02=pos1 > B08=pos2  (ACHI 35414-00 + cranial ACHI → B02 wins)
- MDC 05: F25=pos13            (F03/F04/F05/F06 all rank above F25)
- MDC 06: G13=pos1             (wins over all other MDC 06 intervention ADRGs)

```
FUNCTION step_4_adrg(episode, mdc) → Step4Result:

    intervention_adrgs = KB.get_adrgs(mdc, partition="intervention")
                           ORDERED BY hierarchy_position ASC
    medical_adrgs      = KB.get_adrgs(mdc, partition="medical")

    // Walk hierarchy — first match wins
    FOR adrg IN intervention_adrgs:
        FOR trigger IN adrg.trigger_codes:
            IF trigger IN episode.achi_codes:
                RETURN Step4Result(
                    adrg_code = adrg.adrg_code,
                    partition = "intervention",
                    trace     = "Step 4: " + trigger +
                                " → ADRG " + adrg.adrg_code +
                                " (pos " + str(adrg.hierarchy_position) + ")"
                )

    // No intervention match — medical partition
    medical_adrg = KB.get_medical_adrg(episode.pdx, mdc)

    IF medical_adrg IS NULL:
        RETURN Step4Result(adrg_code="801", partition="medical",
            trace="Step 4: No match → ADRG 801")

    RETURN Step4Result(adrg_code=medical_adrg, partition="medical",
        trace="Step 4: No intervention match → medical " + medical_adrg)
```

---

## STEP 5 — DRG Assignment via ECCS

**Purpose:** Apply Chain of Truth runtime steps:
exclusions → DCL lookup → ECCS → threshold comparison → DRG suffix

**V11.0 locked decisions:**
- Exclusions applied BEFORE DCL lookup (Appendix C ordering)
- Conditional exclusions need the FULL diagnosis list at evaluation time
- DCL lookup is an injected dependency (pre-computed, not derived at runtime)
- ECCS = SUM[DCL_i × 0.86^(i-1)], DCLs sorted descending
- G13Z: ECCS computed for reporting, does not drive DRG assignment
- F25: KnowledgeBaseIncompleteError raised until threshold populated
- errata_applied always included in output

```
FUNCTION step_5_drg(episode, adrg_code) → Step5Result:

    adrg          = KB.get_adrg(adrg_code)
    all_diagnoses = [episode.pdx] + episode.adx
    dcl_entries   = []

    // ── 5A: Apply Appendix C exclusions ──────────────────────────────────
    // CRITICAL: process FULL diagnosis list together.
    // Conditional exclusions (Table C2) depend on what else is present.

    FOR diagnosis IN all_diagnoses:
        is_pdx    = (diagnosis == episode.pdx)
        co_others = [d FOR d IN all_diagnoses WHERE d != diagnosis]

        // Table C1 — unconditional (always excluded)
        IF validation_rules.is_unconditionally_excluded(diagnosis):
            dcl_entries.APPEND(DCLEntry(
                diagnosis_code   = diagnosis,
                dcl_value        = 0,
                is_principal     = is_pdx,
                is_excluded      = True,
                exclusion_type   = "unconditional",
                exclusion_reason = validation_rules.get_exclusion_reason(diagnosis).reason
            ))
            CONTINUE

        // Table C2 — conditional (excluded only if related definitive Dx present)
        IF validation_rules.is_conditionally_excluded(diagnosis, co_others):
            dcl_entries.APPEND(DCLEntry(
                diagnosis_code   = diagnosis,
                dcl_value        = 0,
                is_principal     = is_pdx,
                is_excluded      = True,
                exclusion_type   = "conditional",
                exclusion_reason = "Appendix C Table C2 — definitive diagnosis present"
            ))
            CONTINUE

        // Socioeconomic (Z55-Z65, Z74, Z76 — excluded since V8.0)
        IF validation_rules.is_previously_excluded(diagnosis):
            dcl_entries.APPEND(DCLEntry(
                diagnosis_code   = diagnosis,
                dcl_value        = 0,
                is_principal     = is_pdx,
                is_excluded      = True,
                exclusion_type   = "socioeconomic",
                exclusion_reason = "Excluded since AR-DRG V8.0"
            ))
            CONTINUE

        // ── 5B: DCL lookup ────────────────────────────────────────────────
        // Pre-computed (diagnosis × ADRG) → integer 0-5
        // Source: licensed grouper software / Appendix B aggregation
        dcl = DCL_TABLE.lookup(diagnosis, adrg_code, default=0)

        dcl_entries.APPEND(DCLEntry(
            diagnosis_code   = diagnosis,
            dcl_value        = dcl,
            is_principal     = is_pdx,
            is_excluded      = False,
            exclusion_type   = None,
            exclusion_reason = None
        ))

    // ── 5C: Compute ECCS ─────────────────────────────────────────────────
    // Only non-excluded DCL values contribute
    // Formula: ECCS = SUM[ DCL_i × (0.86)^(i-1) ], DCLs sorted descending

    eligible_dcls = [e.dcl_value FOR e IN dcl_entries
                     WHERE NOT e.is_excluded AND e.dcl_value > 0]
    eccs = validation_rules.compute_eccs(eligible_dcls)

    // ── 5D: Assign DRG suffix ─────────────────────────────────────────────

    // Case 1: Z-suffix (unsplit) — G13 in our KB
    IF adrg.split_profile == "Z":
        RETURN Step5Result(
            drg_code       = adrg_code + "Z",
            eccs           = eccs,
            dcl_entries    = dcl_entries,
            threshold_used = None,
            trace          = "Step 5: Unsplit ADRG → " + adrg_code + "Z" +
                             " (ECCS=" + str(eccs) + " for reporting)"
        )

    // Case 2: Administrative split (LOS, age, separation mode)
    IF adrg.has_administrative_split:
        drg_code = apply_administrative_split(episode, adrg)
        IF drg_code IS NOT NULL:
            RETURN Step5Result(drg_code=drg_code, eccs=eccs,
                dcl_entries=dcl_entries, threshold_used=None,
                trace="Step 5: Administrative split → " + drg_code)

    // Case 3: ECCS-based split — walk end classes, highest complexity first
    FOR end_class IN adrg.end_classes ORDERED BY cost_rank ASC:
        threshold = end_class.eccs_threshold.value

        // Production gate — null threshold fires for F25
        IF threshold IS NULL:
            RAISE KnowledgeBaseIncompleteError(
                "ECCS threshold for " + adrg_code + " is null. " +
                "Populate from AR-DRG V11.0 Definitions Manual " +
                "(Lane Print: ar-drg.laneprint.com.au)."
            )

        IF eccs >= threshold:
            drg_code = adrg_code + end_class.suffix
            RETURN Step5Result(
                drg_code       = drg_code,
                eccs           = eccs,
                dcl_entries    = dcl_entries,
                threshold_used = threshold,
                trace          = "Step 5: ECCS=" + str(eccs) +
                                 " >= " + str(threshold) +
                                 " → " + drg_code
            )

    // Fallback: lowest complexity suffix (no lower bound condition)
    lowest   = LAST element of adrg.end_classes
    drg_code = adrg_code + lowest.suffix
    RETURN Step5Result(
        drg_code       = drg_code,
        eccs           = eccs,
        dcl_entries    = dcl_entries,
        threshold_used = lowest.eccs_threshold.value,
        trace          = "Step 5: ECCS=" + str(eccs) +
                         " below all thresholds → " + drg_code
    )
```

---

## Full orchestration

```
FUNCTION group_episode(episode_json) → GrouperResult:

    episode = parse_and_validate_json(episode_json)
    trace   = []

    // Step 1: Edits
    r1 = step_1_edits(episode)
    IF r1.is_error: RETURN build_error_result(r1)
    trace.APPEND(r1.trace)
    episode = r1.episode  // use cleaned episode

    // Step 2: Pre-MDC
    r2 = step_2_pre_mdc(episode)
    trace.APPEND(r2.trace)

    IF r2.triggered:
        // Skip Step 3
        r5 = step_5_drg(episode, r2.matched_adrg)
        trace.APPEND(r5.trace)
        RETURN build_result(r5, mdc=None, partition="pre_mdc",
                            edit_flags=r1.edit_flags, trace=trace)

    // Step 3: MDC
    r3 = step_3_mdc(episode)
    IF r3.is_error: RETURN build_error_result(r3)
    trace.APPEND(r3.trace)

    // Step 4: ADRG
    r4 = step_4_adrg(episode, r3.mdc)
    trace.APPEND(r4.trace)

    // Step 5: DRG
    r5 = step_5_drg(episode, r4.adrg_code)
    trace.APPEND(r5.trace)

    RETURN GrouperResult(
        episode_id         = episode.episode_id,
        ar_drg_version     = "V11.0",
        ar_drg_code        = r5.drg_code,
        ar_drg_description = KB.get_description(r5.drg_code),
        adrg_code          = r4.adrg_code,
        mdc                = r3.mdc,
        partition          = r4.partition,
        grouping_status    = "SUCCESS",
        eccs               = r5.eccs,
        dcl_contributions  = r5.dcl_entries,
        threshold_used     = r5.threshold_used,
        edit_flags         = r1.edit_flags,
        error_code         = None,
        errata_applied     = ["Errata1_2023-04-01"],
        step_trace         = trace,
        grouped_at         = utcnow()
    )
```

---

## Design decisions — confirm all 12 before Python build

| # | Decision | Source |
|---|----------|--------|
| 1 | Sex conflict TEST removed — FLAG only | V11.0 Section 3.5.2 |
| 2 | R10.2 only remaining sex-routing PDX | V11.0 Section 3.5.1 |
| 3 | Exclusions BEFORE DCL lookup | Appendix C ordering rule |
| 4 | Conditional exclusions need full episode list | Table C2 logic |
| 5 | DCL is injected dependency — pre-computed table | Appendix B / licensed software |
| 6 | F25 raises KnowledgeBaseIncompleteError | Null threshold — production gate |
| 7 | G13Z always assigned; ECCS for reporting only | V11.0 Section 3.3 |
| 8 | ECCS decay 0.86 — global constant | Tech Specs Section 4.5 |
| 9 | Pre-MDC bypasses Step 3 | Final Report Section 1.2 |
| 10 | ADRG 801 is final fallback | V11.0 Section 3.7.4 |
| 11 | FHIR-compatible JSON-Out with DCLEntry[] | Architecture directive + Doc #11 |
| 12 | errata_applied[] on every output record | Versioning immutability principle |

---

## Open blockers — must resolve before production

| Item | Status | Resolution |
|------|--------|------------|
| F25 ECCS threshold | null — hard block | Purchase Definitions Manual (Lane Print) |
| Full DCL lookup table | Not public | License grouper software OR empirical derivation |
| Appendix C full Table C1+C2 | 7 of 47 confirmed | Purchase Definitions Manual Volume 3 |
| MDC_PDX_LOOKUP (Appendix A) | Not public | Purchase Definitions Manual |
| PRE_MDC_ADRG_LIST | Not public | Purchase Definitions Manual |

---
*Deliverable 3.1 complete — awaiting sign-off before grouper.py build begins*
