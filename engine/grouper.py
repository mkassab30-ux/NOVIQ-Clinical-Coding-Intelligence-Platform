"""
NOVIQ Engine — AR-DRG V11.0 Grouper
=====================================
Phase 3, Deliverable 3.2

Architecture: JSON-In / JSON-Out — zero UI coupling, zero DB calls.
Entry point: ARDRGGrouper.group_episode(episode_dict) → dict
Versioning: V11.0, Errata 1 (2023-04-01)
FHIR: Output schema compatible with Encounter/Observation extension
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validation_rules import (
    DCLExclusionKnowledgeBase,
    KnowledgeBaseIncompleteError,
    compute_eccs,
    compute_eccs_with_trace,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AR_DRG_VERSION = "V11.0"
ERRATA_APPLIED = ["Errata1_2023-04-01"]
MODULE_VERSION = "1.0.0"

SEX_ROUTING_PDX = "R10.2"

ERROR_DRG_UNGROUPABLE = "960Z"
ERROR_DRG_BAD_PDX = "961Z"
ERROR_DRG_NEONATAL = "963Z"

DEFAULT_KB_PATH = Path(__file__).parent / "ar_drg_kb_seed_v11_new_adrgs.json"
DEFAULT_EXCL_PATH = Path(__file__).parent / "dcl_exclusions.json"

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

def _dcl_entry(code: str, dcl: int, is_pdx: bool,
               excluded: bool, excl_type: str | None,
               excl_reason: str | None) -> dict:
    return {
        "diagnosis_code": code,
        "dcl_value": dcl,
        "is_principal": is_pdx,
        "is_excluded": excluded,
        "exclusion_type": excl_type,
        "exclusion_reason": excl_reason,
    }

def _error_result(episode_id: str, drg_code: str,
                  reason: str, trace: list[str]) -> dict:
    return {
        "episode_id": episode_id,
        "ar_drg_version": AR_DRG_VERSION,
        "ar_drg_code": drg_code,
        "ar_drg_description": _error_description(drg_code),
        "adrg_code": drg_code[:3],
        "mdc": None,
        "partition": "error",
        "grouping_status": "ERROR",
        "eccs": 0.0,
        "dcl_contributions": [],
        "threshold_used": None,
        "edit_flags": [],
        "error_code": drg_code,
        "errata_applied": ERRATA_APPLIED,
        "step_trace": trace + [f"TERMINAL: {reason}"],
        "grouped_at": _now(),
        "module_version": MODULE_VERSION,
    }

def _error_description(drg_code: str) -> str:
    return {
        ERROR_DRG_UNGROUPABLE: "Ungroupable",
        ERROR_DRG_BAD_PDX: "Unacceptable Principal Diagnosis",
        ERROR_DRG_NEONATAL: "Neonatal Diagnosis Not Consistent with Age/Weight",
    }.get(drg_code, "Error")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Knowledge Base loader
# ---------------------------------------------------------------------------

class GrouperKnowledgeBase:
    def __init__(self, kb_path: Path = DEFAULT_KB_PATH) -> None:
        if not kb_path.exists():
            raise FileNotFoundError(f"KB not found: {kb_path}")
        with open(kb_path, encoding="utf-8") as f:
            self._raw = json.load(f)
        self._adrgs: dict[str, dict] = self._raw.get("adrgs", {})
        self._version = self._raw.get("_meta", {}).get(
            "versioning", {}).get("ar_drg_version", "UNKNOWN")
        self._mdc_pdx_lookup: dict[str, str] = self._raw.get("mdc_pdx_lookup", {})

    def get_adrg(self, adrg_code: str) -> dict | None:
        return self._adrgs.get(adrg_code.upper())

    def get_description(self, drg_code: str) -> str:
        adrg_code = drg_code[:3].upper()
        suffix = drg_code[3:].upper() if len(drg_code) > 3 else ""
        adrg = self._adrgs.get(adrg_code)
        if not adrg:
            return drg_code
        for ec in adrg.get("split_profile", {}).get("end_classes", []):
            if ec.get("suffix", "").upper() == suffix:
                return ec.get("drg_description", drg_code)
        return adrg.get("adrg_description", drg_code)

    @property
    def version(self) -> str:
        return self._version

# ---------------------------------------------------------------------------
# DCL Table
# ---------------------------------------------------------------------------

class DCLTable:
    PRODUCTION_READY = False

    def __init__(self, table_path: Path | None = None) -> None:
        self._table: dict[tuple[str, str], int] = {}
        if table_path and table_path.exists():
            with open(table_path, encoding="utf-8") as f:
                raw = json.load(f)
            for key, val in raw.items():
                parts = key.split(":", 1)
                if len(parts) == 2:
                    self._table[(parts[1].upper(), parts[0].upper())] = int(val)
            self.PRODUCTION_READY = True

    def lookup(self, diagnosis_code: str, adrg_code: str) -> int:
        key = (diagnosis_code.strip().upper(), adrg_code.strip().upper())
        return self._table.get(key, 0)

# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def _step1_edits(episode: dict) -> tuple[bool, str | None, list[str], dict]:
    flags = []
    ep = dict(episode)

    valid_sex = {"Male", "Female", "Other", "Unknown"}
    if ep.get("patient_sex") not in valid_sex:
        return False, ERROR_DRG_UNGROUPABLE, [], ep

    pdx = (ep.get("pdx") or "").strip().upper()
    if not pdx:
        return False, ERROR_DRG_UNGROUPABLE, [], ep
    ep["pdx"] = pdx

    if not _is_plausible_icd_code(pdx):
        return False, ERROR_DRG_BAD_PDX, [], ep

    if _is_neonatal_pdx(pdx):
        age = ep.get("patient_age", 0) or 0
        if age > 0:
            return False, ERROR_DRG_NEONATAL, [], ep
        weight = ep.get("admission_weight")
        if weight is not None and not _weight_consistent(pdx, weight):
            return False, ERROR_DRG_NEONATAL, [], ep

    clean_adx = []
    for code in (ep.get("adx") or []):
        code = code.strip().upper()
        if _is_plausible_icd_code(code):
            clean_adx.append(code)
        else:
            flags.append(f"ADX_STRIPPED: {code} — invalid code removed")
    ep["adx"] = clean_adx

    clean_achi = []
    for code in (ep.get("achi_codes") or []):
        code = code.strip().upper()
        if _is_plausible_achi_code(code):
            clean_achi.append(code)
        else:
            flags.append(f"ACHI_STRIPPED: {code} — invalid code removed")
    ep["achi_codes"] = clean_achi

    for code in clean_adx + clean_achi:
        if _sex_conflicts(ep.get("patient_sex", ""), code):
            flags.append(
                f"SEX_CONFLICT_WARN: {code} conflicts with "
                f"sex={ep['patient_sex']} "
                f"[warning only — does not affect grouping in V11.0]"
            )

    for code in ([pdx] + clean_adx):
        if _age_conflicts(ep.get("patient_age", 0) or 0, code):
            flags.append(f"AGE_CONFLICT_WARN: {code} conflicts with age={ep.get('patient_age')}")

    return True, None, flags, ep

def _step2_pre_mdc(episode: dict,
                   pre_mdc_list: list[dict]) -> tuple[bool, str | None]:
    achi_codes = set(episode.get("achi_codes") or [])
    for adrg in sorted(pre_mdc_list, key=lambda a: a.get("hierarchy_position", 99)):
        for trigger in adrg.get("trigger_codes", []):
            if trigger.upper() in achi_codes:
                return True, adrg["adrg_code"]
    return False, None

def _step3_mdc(episode: dict,
               mdc_pdx_lookup: dict[str, str]) -> tuple[str | None, str | None]:
    pdx = episode["pdx"]
    if pdx == SEX_ROUTING_PDX:
        sex = episode.get("patient_sex", "Unknown")
        if sex in ("Male", "Other", "Unknown"):
            return "12", None
        else:
            return "13", None
    mdc = mdc_pdx_lookup.get(pdx.upper())
    if mdc is None:
        return None, ERROR_DRG_UNGROUPABLE
    return mdc, None

def _step4_adrg(episode: dict,
                mdc: str,
                kb: GrouperKnowledgeBase,
                mdc_adrg_registry: dict) -> tuple[str, str]:
    achi_codes = set(episode.get("achi_codes") or [])
    mdc_adrgs = mdc_adrg_registry.get(mdc, [])
    intervention_adrgs = sorted(
        [a for a in mdc_adrgs if a.get("partition") == "intervention"],
        key=lambda a: a.get("hierarchy_position", 99)
    )

    for adrg_def in intervention_adrgs:
        for trigger in adrg_def.get("trigger_codes", []):
            if trigger.upper() in achi_codes:
                return adrg_def["adrg_code"], "intervention"

    medical_adrgs = [a for a in mdc_adrgs if a.get("partition") == "medical"]
    pdx = episode["pdx"]
    for adrg_def in medical_adrgs:
        for pdx_range in adrg_def.get("pdx_ranges", []):
            if _pdx_in_range(pdx, pdx_range):
                return adrg_def["adrg_code"], "medical"

    return "801", "medical"

def _step5_drg(episode: dict,
               adrg_code: str,
               kb: GrouperKnowledgeBase,
               excl_kb: DCLExclusionKnowledgeBase,
               dcl_table: DCLTable) -> dict:
    adrg = kb.get_adrg(adrg_code)
    if adrg is None:
        return {
            "drg_code": adrg_code + "Z",
            "eccs": 0.0,
            "dcl_contributions": [],
            "threshold_used": None,
            "trace": f"Step 5: ADRG {adrg_code} not in KB → {adrg_code}Z"
        }

    all_diagnoses = [episode["pdx"]] + list(episode.get("adx") or [])
    dcl_entries = []

    for diagnosis in all_diagnoses:
        is_pdx = (diagnosis == episode["pdx"])
        co_others = [d for d in all_diagnoses if d != diagnosis]

        if excl_kb.is_unconditionally_excluded(diagnosis):
            reason = excl_kb.get_exclusion_entry(diagnosis)
            dcl_entries.append(_dcl_entry(
                diagnosis, 0, is_pdx, True, "unconditional",
                reason.get("exclusion_reason", "Appendix C Table C1")
                if reason else "Appendix C Table C1 — unconditional exclusion"
            ))
            continue

        if excl_kb.is_conditionally_excluded(diagnosis, co_others):
            dcl_entries.append(_dcl_entry(
                diagnosis, 0, is_pdx, True, "conditional",
                "Appendix C Table C2 — definitive diagnosis co-present in episode"
            ))
            continue

        if excl_kb.is_previously_excluded(diagnosis):
            dcl_entries.append(_dcl_entry(
                diagnosis, 0, is_pdx, True, "socioeconomic",
                "Socioeconomic code excluded from ECC Model since AR-DRG V8.0"
            ))
            continue

        dcl = dcl_table.lookup(diagnosis, adrg_code)
        dcl_entries.append(_dcl_entry(
            diagnosis, dcl, is_pdx, False, None, None
        ))

    eligible_dcls = [
        e["dcl_value"] for e in dcl_entries
        if not e["is_excluded"] and e["dcl_value"] > 0
    ]
    eccs = compute_eccs(eligible_dcls)

    split_profile = adrg.get("split_profile", {})

    if split_profile.get("profile") == "Z":
        drg_code = adrg_code + "Z"
        return {
            "drg_code": drg_code,
            "eccs": eccs,
            "dcl_contributions": dcl_entries,
            "threshold_used": None,
            "trace": (
                f"Step 5: ADRG {adrg_code} is unsplit → {drg_code} "
                f"(ECCS={eccs} computed for clinical reporting)"
            )
        }

    if split_profile.get("has_administrative_split"):
        drg_code = _apply_administrative_split(episode, adrg, adrg_code)
        if drg_code:
            return {
                "drg_code": drg_code,
                "eccs": eccs,
                "dcl_contributions": dcl_entries,
                "threshold_used": None,
                "trace": f"Step 5: Administrative split → {drg_code}"
            }

    end_classes = sorted(
        split_profile.get("end_classes", []),
        key=lambda ec: ec.get("cost_rank", 99)
    )

    for end_class in end_classes:
        threshold_obj = end_class.get("eccs_threshold", {})

        if not isinstance(threshold_obj, dict):
            threshold_obj = {}

        threshold_val = threshold_obj.get("value")
        op = threshold_obj.get("operator", ">=")

        if threshold_val is None and end_class.get("suffix") != end_classes[-1].get("suffix"):
            raise KnowledgeBaseIncompleteError(
                f"ECCS threshold for {adrg_code} "
                f"(suffix {end_class.get('suffix')}) is null in Knowledge Base. "
                f"Populate from AR-DRG V11.0 Definitions Manual "
                f"(Lane Print: ar-drg.laneprint.com.au). "
                f"NOVIQ Engine cannot assign {adrg_code} DRG until resolved."
            )

        if threshold_val is not None:
            if op == ">=" and eccs >= threshold_val:
                drg_code = adrg_code + end_class["suffix"]
                return {
                    "drg_code": drg_code,
                    "eccs": eccs,
                    "dcl_contributions": dcl_entries,
                    "threshold_used": threshold_val,
                    "trace": (
                        f"Step 5: ECCS={eccs} >= threshold_val} "
                        f"→ {drg_code}"
                    )
                }

    lowest = end_classes[-1]
    drg_code = adrg_code + lowest["suffix"]
    return {
        "drg_code": drg_code,
        "eccs": eccs,
        "dcl_contributions": dcl_entries,
        "threshold_used": lowest.get("eccs_threshold", {}).get("value"),
        "trace": (
            f"Step 5: ECCS={eccs} below all thresholds "
            f"→ {drg_code} (lowest complexity)"
        )
    }

# ---------------------------------------------------------------------------
# Administrative split helper
# ---------------------------------------------------------------------------

def _apply_administrative_split(episode: dict, adrg: dict,
                                adrg_code: str) -> str | None:
    admin_rules = adrg.get("split_profile", {}).get("administrative_rules", [])
    for rule in admin_rules:
        variable = rule.get("variable")
        if variable == "los_days":
            if episode.get("los_days", 0) < rule.get("threshold"):
                return adrg_code + rule.get("suffix")
        elif variable == "separation_mode":
            if episode.get("separation_mode") == rule.get("value"):
                return adrg_code + rule.get("suffix")
        elif variable == "age":
            if episode.get("patient_age", 0) < rule.get("threshold"):
                return adrg_code + rule.get("suffix")
    return None

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _is_plausible_icd_code(code: str) -> bool:
    if not code or len(code) < 3 or len(code) > 8:
        return False
    return code[0].isalpha() and code[1:3].isdigit()

def _is_plausible_achi_code(code: str) -> bool:
    if not code:
        return False
    return any(c.isdigit() for c in code)

def _is_neonatal_pdx(pdx: str) -> bool:
    return pdx.startswith("P") or pdx.startswith("Z38")

def _weight_consistent(pdx: str, weight_grams: int) -> bool:
    return weight_grams > 0

def _sex_conflicts(sex: str, code: str) -> bool:
    return False

def _age_conflicts(age: int, code: str) -> bool:
    return False

def _pdx_in_range(pdx: str, pdx_range: str) -> bool:
    if "-" in pdx_range:
        start, end = pdx_range.split("-", 1)
        return start.upper() <= pdx.upper() <= end.upper() + "Z"
    return pdx.upper().startswith(pdx_range.upper())

# ---------------------------------------------------------------------------
# Main grouper class
# ---------------------------------------------------------------------------

class ARDRGGrouper:
    def __init__(
        self,
        kb_path: Path = DEFAULT_KB_PATH,
        excl_path: Path = DEFAULT_EXCL_PATH,
        dcl_table: DCLTable | None = None,
        pre_mdc_list: list[dict] | None = None,
        mdc_pdx_lookup: dict[str, str] | None = None,
        mdc_adrg_registry: dict[str, list] | None = None,
    ) -> None:

        self.kb = GrouperKnowledgeBase(kb_path)
        self.excl_kb = DCLExclusionKnowledgeBase(excl_path)
        self.dcl_table = dcl_table or DCLTable()

        self.pre_mdc_list = pre_mdc_list or []
        self.mdc_pdx_lookup = mdc_pdx_lookup or self.kb._mdc_pdx_lookup or {}
        self.mdc_adrg_registry = mdc_adrg_registry or self._build_registry_from_kb()

    def _build_registry_from_kb(self) -> dict[str, list]:
        registry: dict[str, list] = {}
        for adrg_code, adrg_def in self.kb._adrgs.items():
            mdc = adrg_def.get("mdc")
            if not mdc:
                continue
            if mdc not in registry:
                registry[mdc] = []
            registry[mdc].append({
                "adrg_code": adrg_code,
                "partition": adrg_def.get("partition", "intervention"),
                "hierarchy_position": adrg_def.get("hierarchy", {}).get("position", 99),
                "trigger_codes": [
                    t["achi_code"]
                    for t in adrg_def.get("trigger_codes", [])
                    if t.get("role") == "trigger"
                ],
                "pdx_ranges": adrg_def.get("pdx_ranges", []),
            })
        return registry

    def group_episode(self, episode: dict) -> dict:
        episode_id = episode.get("episode_id", "UNKNOWN")
        trace: list[str] = []

        passed, err_drg, edit_flags, episode = _step1_edits(episode)
        if not passed:
            return _error_result(episode_id, err_drg,
                               f"Step 1 edit failure → {err_drg}", trace)
        trace.append(
            f"Step 1 PASS — PDX={episode['pdx']}, "
            f"{len(episode.get('adx',[]))} ADX, "
            f"{len(episode.get('achi_codes',[]))} ACHI codes validated"
        )

        pre_triggered, pre_adrg = _step2_pre_mdc(episode, self.pre_mdc_list)
        if pre_triggered:
            trace.append(
                f"Step 2: Pre-MDC trigger → ADRG {pre_adrg} "
                f"— MDC assignment bypassed"
            )
            r5 = _step5_drg(episode, pre_adrg, self.kb,
                           self.excl_kb, self.dcl_table)
            trace.append(r5["trace"])
            return self._build_result(
                episode_id, r5, mdc=None,
                partition="pre_mdc", edit_flags=edit_flags, trace=trace
            )
        trace.append("Step 2: No Pre-MDC trigger → MDC assignment proceeds")

        mdc, err_drg = _step3_mdc(episode, self.mdc_pdx_lookup)
        if err_drg:
            return _error_result(episode_id, err_drg,
                               f"Step 3 MDC lookup failed for PDX={episode['pdx']}",
                               trace)

        if episode["pdx"] == SEX_ROUTING_PDX:
            trace.append(
                f"Step 3: R10.2 sex-routing → "
                f"sex={episode.get('patient_sex')} → MDC {mdc}"
            )
        else:
            trace.append(f"Step 3: PDX={episode['pdx']} → MDC {mdc}")

        adrg_code, partition = _step4_adrg(
            episode, mdc, self.kb, self.mdc_adrg_registry
        )
        trace.append(
            f"Step 4: ADRG={adrg_code} ({partition})"
        )

        r5 = _step5_drg(episode, adrg_code, self.kb,
                       self.excl_kb, self.dcl_table)
        trace.append(r5["trace"])

        return self._build_result(
            episode_id, r5, mdc=mdc,
            partition=partition, edit_flags=edit_flags, trace=trace
        )

    def _build_result(self, episode_id: str, r5: dict,
                      mdc: str | None, partition: str,
                      edit_flags: list[str], trace: list[str]) -> dict:
        drg_code = r5["drg_code"]
        return {
            "episode_id": episode_id,
            "ar_drg_version": AR_DRG_VERSION,
            "ar_drg_code": drg_code,
            "ar_drg_description": self.kb.get_description(drg_code),
            "adrg_code": drg_code[:3],
            "mdc": mdc,
            "partition": partition,
            "grouping_status": "SUCCESS",
            "eccs": r5["eccs"],
            "dcl_contributions": r5["dcl_contributions"],
            "threshold_used": r5["threshold_used"],
            "edit_flags": edit_flags,
            "error_code": None,
            "errata_applied": ERRATA_APPLIED,
            "step_trace": trace,
            "grouped_at": _now(),
            "module_version": MODULE_VERSION,
        }

# ---------------------------------------------------------------------------
# Input / Output schemas (documentation)
# ---------------------------------------------------------------------------

INPUT_SCHEMA = {
    "episode_id": "string",
    "patient_age": "integer — years",
    "patient_sex": "Male | Female | Other | Unknown",
    "admission_weight": "integer | null — grams, neonates only",
    "same_day": "boolean",
    "separation_mode": "string",
    "los_days": "integer",
    "pdx": "string — principal ICD-10-AM code",
    "adx": ["list of additional ICD-10-AM codes"],
    "achi_codes": ["list of ACHI codes"],
    "hours_mech_vent": "integer | null",
    "care_type": "string — 01=Acute 07=Newborn 11=MentalHealth",
}

OUTPUT_SCHEMA = {
    "episode_id": "string",
    "ar_drg_version": "V11.0",
    "ar_drg_code": "string — e.g. B08A, G13Z",
    "ar_drg_description": "string",
    "adrg_code": "string — e.g. B08",
    "mdc": "string | null",
    "partition": "intervention | medical | pre_mdc | error",
    "grouping_status": "SUCCESS | ERROR | WARNING",
    "eccs": "float",
    "dcl_contributions": [{"diagnosis_code": "str", "dcl_value": "int",
                           "is_principal": "bool", "is_excluded": "bool",
                           "exclusion_type": "str|null",
                           "exclusion_reason": "str|null"}],
    "threshold_used": "float | null",
    "edit_flags": ["list of warning strings"],
    "error_code": "string | null",
    "errata_applied": ["list of errata identifiers"],
    "step_trace": ["full audit trail — one entry per step"],
    "grouped_at": "ISO 8601 UTC timestamp",
    "module_version": "string",
}

# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    grouper = ARDRGGrouper()

    test_g13 = {
        "episode_id": "TEST-G13-001",
        "patient_age": 58,
        "patient_sex": "Female",
        "admission_weight": None,
        "same_day": False,
        "separation_mode": "discharge_home",
        "los_days": 12,
        "pdx": "C48.1",
        "adx": ["E11.9", "I10", "E61.1", "Z59.0"],
        "achi_codes": ["96211-00"],
        "hours_mech_vent": None,
        "care_type": "01",
    }

    mdc_lookup = {"I63.3": "01", "I63.4": "01", "C48.1": "06"}
    mdc_registry = {
        "01": [{
            "adrg_code": "B08",
            "partition": "intervention",
            "hierarchy_position": 2,
            "trigger_codes": ["35414-00"],
            "pdx_ranges": [],
        }],
        "06": [{
            "adrg_code": "G13",
            "partition": "intervention",
            "hierarchy_position": 1,
            "trigger_codes": ["96211-00"],
            "pdx_ranges": [],
        }]
    }

    grouper_with_lookup = ARDRGGrouper(
        mdc_pdx_lookup=mdc_lookup,
        mdc_adrg_registry=mdc_registry,
    )

    test_b08 = {
        "episode_id": "TEST-B08-001",
        "patient_age": 72,
        "patient_sex": "Male",
        "admission_weight": None,
        "same_day": False,
        "separation_mode": "discharge_home",
        "los_days": 4,
        "pdx": "I63.3",
        "adx": ["I10", "E11.9"],
        "achi_codes": ["35414-00"],
        "hours_mech_vent": None,
        "care_type": "01",
    }

    test_g13_full = {**test_g13}

    print("=" * 60)
    print("NOVIQ Engine — grouper.py smoke test")
    print("=" * 60)

    for label, episode, grpr in [
        ("G13Z — Peritonectomy (unsplit)", test_g13_full, grouper_with_lookup),
        ("B08 — Endovascular Clot Retrieval", test_b08, grouper_with_lookup),
    ]:
        result = grpr.group_episode(episode)
        print(f"\nTest: {label}")
        print(f" DRG: {result['ar_drg_code']}")
        print(f" ECCS: {result['eccs']}")
        print(f" Threshold: {result['threshold_used']}")
        print(f" Status: {result['grouping_status']}")
        print(f" DCL entries: {len(result['dcl_contributions'])}")
        excl = [e for e in result['dcl_contributions'] if e['is_excluded']]
        print(f" Excluded: {len(excl)} ({[e['diagnosis_code'] for e in excl]})")
        print(f" Trace:")
        for t in result['step_trace']:
            print(f" → {t}")

    test_bad_pdx = {
        "episode_id": "TEST-961Z",
        "patient_age": 45, "patient_sex": "Female",
        "admission_weight": None, "same_day": False,
        "separation_mode": "discharge_home", "los_days": 2,
        "pdx": "INVALID!!", "adx": [], "achi_codes": [],
        "hours_mech_vent": None, "care_type": "01",
    }
    result_bad = grouper.group_episode(test_bad_pdx)
    print(f"\nTest: 961Z — Invalid PDX")
    print(f" DRG: {result_bad['ar_drg_code']}")
    print(f" Status: {result_bad['grouping_status']}")
    print(f" Error: {result_bad['error_code']}")

    test_r102_male = {
        "episode_id": "TEST-R102-M",
        "patient_age": 35, "patient_sex": "Male",
        "admission_weight": None, "same_day": False,
        "separation_mode": "discharge_home", "los_days": 1,
        "pdx": "R10.2", "adx": [], "achi_codes": [],
        "hours_mech_vent": None, "care_type": "01",
    }
    grp_r102 = ARDRGGrouper(mdc_pdx_lookup={"R10.2": "12"})
    result_r102 = grp_r102.group_episode(test_r102_male)
    print(f"\nTest: R10.2 sex-routing (Male)")
    print(f" MDC assigned: {result_r102['mdc']} (expected: 12)")
    print(f" Trace: {result_r102['step_trace'][2]}")

    print("\n✓ All smoke tests complete")
