"""
NOVIQ Engine — ICD-10-AM Validator
=====================================
4-Layer validation per Dr. Mohamed Kassab + ACS 0050

Layer 1: Format check       — regex
Layer 2: MDC existence      — 37K lookup  
Layer 3: PDX acceptability  — ACS 0050 + Appendix C rules
Layer 4: Semantic fallback  — Claude API for unknown codes
"""
from __future__ import annotations
import re, json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

VALID            = "VALID"
INVALID_FORMAT   = "INVALID_FORMAT"
INVALID_PDX      = "INVALID_PDX"
WARNING_SYMPTOM  = "WARNING_SYMPTOM"
WARNING_ZCODE    = "WARNING_ZCODE"
WARNING_UNSPEC   = "WARNING_UNSPEC"
WARNING_MANIFEST = "WARNING_MANIFEST"
UNVERIFIED       = "UNVERIFIED"

ICD_PATTERN          = re.compile(r"^[A-Z]\d{2}(\.[0-9A-Z]{1,2})?$")
NEVER_PDX_PREFIXES   = {"V","W","X","Y"}
ALWAYS_QUERY_R       = {"R69","R99"}
ACCEPTABLE_Z_PDX     = {
    "Z49.1","Z49.2","Z51.1","Z51.11","Z51.12","Z51.0",
    "Z50.1","Z50.2","Z50.3","Z50.4","Z50.5",
    "Z34","Z38","Z29.1",
}
NEVER_Z_PDX_PREFIXES = {"Z00","Z01","Z02","Z03","Z04","Z08","Z09","Z13","Z21"}
MANIFESTATION_PFXS   = {"H28","N08","G53","G63","G73","M36","E35"}

HIGH_IMPACT_UNSPEC = {
    "K80.9": ("Is there associated cholecystitis or obstruction?",
              ["K80.0","K80.1","K80.2","K80.3"]),
    "I50.9": ("Specify HF type: systolic/diastolic/combined?",
              ["I50.0","I50.1","I50.2","I50.3"]),
    "J18.9": ("Causative organism identified? Affects DRG weight.",
              ["J15.0","J13","J18.0","J18.1"]),
    "A41.9": ("Sepsis source/organism identified?",
              ["A41.0","A41.1","A41.2","A41.51"]),
    "N18.9": ("Specify CKD stage (1–5)?",
              ["N18.1","N18.2","N18.3","N18.4","N18.5","N18.6"]),
    "J44.9": ("Exacerbation (J44.1) or stable (J44.0)?",
              ["J44.0","J44.1"]),
    "C80.9": ("Primary malignancy site identified?",
              ["Code primary site specifically"]),
    "S09.9": ("Specify nature of head injury.",
              ["S06.0","S06.1","S06.2","S06.3"]),
    "T14.9": ("Specify injury nature and site.",
              ["S/T code with specific site"]),
    "E11.9": ("Any diabetes complications documented?",
              ["E11.0","E11.1","E11.2","E11.3","E11.4","E11.5","E11.6"]),
}

@dataclass
class CodeValidation:
    code:              str
    result:            str
    is_valid:          bool
    is_acceptable_pdx: bool = True
    message:           str  = ""
    query_template:    Optional[str] = None
    suggested_codes:   list = field(default_factory=list)
    mdc:               Optional[str] = None
    layer_reached:     int  = 0

@dataclass
class EpisodeValidation:
    pdx_validation:   CodeValidation
    adx_validations:  list
    achi_validations: list
    episode_valid:    bool
    submission_ready: bool
    error_drg_risk:   bool
    flags:            list
    summary:          dict

class ICDValidator:
    def __init__(self, kb_dir, anthropic_client=None):
        self._kb_dir    = Path(kb_dir)
        self._anthropic = anthropic_client
        self._mdc: dict = {}
        self._load()

    def _load(self):
        p = self._kb_dir / "ar_drg_kb_seed_v11_new_adrgs.json"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                self._mdc = json.load(f).get("mdc_pdx_lookup", {})

    # Layer 1
    def _l1(self, code: str):
        if not code or not isinstance(code, str): return False, "Empty code"
        if not ICD_PATTERN.match(code.strip().upper()):
            return False, f"Invalid ICD-10-AM format: '{code}'"
        return True, ""

    # Layer 2
    def _l2(self, code: str):
        mdc = self._mdc.get(code)
        if mdc: return mdc, True
        for l in [5,4,3,2]:
            mdc = self._mdc.get(code[:l])
            if mdc: return mdc, True
        return None, False

    # Layer 3
    def _l3_pdx(self, code: str) -> CodeValidation:
        p1 = code[0]; p3 = code[:3]

        if p1 in NEVER_PDX_PREFIXES:
            return CodeValidation(code=code, result=INVALID_PDX,
                is_valid=True, is_acceptable_pdx=False, layer_reached=3,
                message=(f"{code} is an external cause code (ACS 2001/2005). "
                         f"NEVER acceptable as PDX — will produce 961Z. "
                         f"Use S/T injury code as PDX."))

        if p1 == "R":
            is_ok = p3 not in ALWAYS_QUERY_R
            return CodeValidation(code=code, result=WARNING_SYMPTOM,
                is_valid=True, is_acceptable_pdx=is_ok, layer_reached=3,
                query_template="pdx_symptom_only",
                message=(f"{code} is a symptom code. Acceptable as PDX only if no "
                         f"definitive diagnosis reached. Query physician if possible."))

        if p1 == "Z":
            if any(code.startswith(p) for p in NEVER_Z_PDX_PREFIXES):
                return CodeValidation(code=code, result=WARNING_ZCODE,
                    is_valid=True, is_acceptable_pdx=False, layer_reached=3,
                    message=(f"{code} is not acceptable as PDX (ACS 0050). "
                             f"Use the condition found on screening."))
            if code in ACCEPTABLE_Z_PDX or code[:4] in ACCEPTABLE_Z_PDX:
                return CodeValidation(code=code, result=VALID,
                    is_valid=True, is_acceptable_pdx=True, layer_reached=3,
                    message=f"{code} is an acceptable Z-code PDX.")
            return CodeValidation(code=code, result=WARNING_ZCODE,
                is_valid=True, is_acceptable_pdx=True, layer_reached=3,
                query_template="pdx_query_diagnosis",
                message=(f"{code}: verify against acceptable PDX list "
                         f"(ACS 0001 + Appendix C)."))

        if any(code.startswith(p) for p in MANIFESTATION_PFXS):
            return CodeValidation(code=code, result=WARNING_MANIFEST,
                is_valid=True, is_acceptable_pdx=True, layer_reached=3,
                query_template="pdx_query_diagnosis",
                message=(f"{code} is a manifestation (*) code. Acceptable as PDX "
                         f"when admission is for the manifestation. Confirm etiology "
                         f"documented as secondary diagnosis."))

        if code in HIGH_IMPACT_UNSPEC:
            qmsg, suggs = HIGH_IMPACT_UNSPEC[code]
            return CodeValidation(code=code, result=WARNING_UNSPEC,
                is_valid=True, is_acceptable_pdx=True, layer_reached=3,
                query_template="adx_elevated_value", suggested_codes=suggs,
                message=(f"{code} is unspecified. Greater specificity improves "
                         f"DRG weight. Query: {qmsg}"))

        return CodeValidation(code=code, result=VALID,
            is_valid=True, is_acceptable_pdx=True, layer_reached=3)

    # Layer 4
    def _l4(self, code: str) -> CodeValidation:
        if not self._anthropic:
            return CodeValidation(code=code, result=UNVERIFIED,
                is_valid=True, is_acceptable_pdx=True, layer_reached=4,
                message=(f"{code} not in MDC lookup. Enable Claude API for "
                         f"semantic validation. Verify manually."))
        try:
            r = self._anthropic.messages.create(
                model="claude-sonnet-4-20250514", max_tokens=200,
                messages=[{"role":"user","content":(
                    f"Is '{code}' a valid ICD-10-AM 11th Edition code? "
                    f"Reply ONLY with JSON: {{\"valid\":true/false,"
                    f"\"description\":\"...\",\"acceptable_as_pdx\":true/false}}")}])
            d = json.loads(r.content[0].text.strip().replace("```json","").replace("```",""))
            return CodeValidation(code=code,
                result=VALID if d.get("valid") else INVALID_FORMAT,
                is_valid=bool(d.get("valid")),
                is_acceptable_pdx=d.get("acceptable_as_pdx", True),
                message=d.get("description",""), layer_reached=4)
        except Exception as e:
            return CodeValidation(code=code, result=UNVERIFIED,
                is_valid=True, is_acceptable_pdx=True, layer_reached=4,
                message=f"Semantic validation error: {e}")

    def validate_code(self, code: str, is_pdx: bool = False) -> CodeValidation:
        code = (code or "").strip().upper()
        ok, msg = self._l1(code)
        if not ok:
            return CodeValidation(code=code, result=INVALID_FORMAT,
                is_valid=False, is_acceptable_pdx=False,
                message=msg, layer_reached=1)
        mdc, found = self._l2(code)
        if is_pdx:
            result = self._l3_pdx(code)
            result.mdc = mdc
            return result
        if not found:
            result = self._l4(code)
            result.mdc = mdc
            return result
        return CodeValidation(code=code, result=VALID,
            is_valid=True, is_acceptable_pdx=True, mdc=mdc, layer_reached=2)

    def _val_achi(self, code: str) -> CodeValidation:
        p = re.compile(r"^\d{5}-\d{2}$")
        if not code or not p.match(code.strip()):
            return CodeValidation(code=code, result=INVALID_FORMAT,
                is_valid=False, is_acceptable_pdx=True, layer_reached=1,
                message=f"Invalid ACHI format: '{code}'. Expected: 00000-00")
        return CodeValidation(code=code, result=VALID,
            is_valid=True, is_acceptable_pdx=True, layer_reached=1)

    def validate_episode(self, episode: dict) -> EpisodeValidation:
        pdx_v   = self.validate_code(episode.get("pdx",""), is_pdx=True)
        adx_vs  = [self.validate_code(c) for c in episode.get("adx",[])]
        achi_vs = [self._val_achi(c)     for c in episode.get("achi_codes",[])]

        flags = []
        drg_risk = False

        def _flag(sev, v, typ):
            if v.result != VALID:
                flags.append({
                    "severity": sev, "code": v.code, "type": typ,
                    "result": v.result, "message": v.message,
                    "query_template": v.query_template,
                    "suggested_codes": v.suggested_codes,
                })

        if pdx_v.result == INVALID_PDX or not pdx_v.is_valid:
            drg_risk = True
            flags.append({"severity":"CRITICAL","code":pdx_v.code,
                "type":"PDX","result":pdx_v.result,"message":pdx_v.message,
                "engine_action":"Will produce 961Z — must change PDX"})
        elif pdx_v.result != VALID:
            _flag("HIGH", pdx_v, "PDX")

        for v in adx_vs:
            _flag("MEDIUM" if v.result == WARNING_UNSPEC else "HIGH", v, "ADX")
        for v in achi_vs:
            if not v.is_valid:
                _flag("MEDIUM", v, "ACHI")

        crit = sum(1 for f in flags if f["severity"]=="CRITICAL")
        high = sum(1 for f in flags if f["severity"]=="HIGH")
        med  = sum(1 for f in flags if f["severity"]=="MEDIUM")

        return EpisodeValidation(
            pdx_validation=pdx_v, adx_validations=adx_vs,
            achi_validations=achi_vs,
            episode_valid=not drg_risk,
            submission_ready=(crit==0 and not drg_risk),
            error_drg_risk=drg_risk, flags=flags,
            summary={"total_flags":len(flags),"critical":crit,
                     "high":high,"medium":med,
                     "submission_ready":crit==0 and not drg_risk,
                     "error_drg_risk":drg_risk,
                     "pdx_result":pdx_v.result,"pdx_mdc":pdx_v.mdc})

    def to_dict(self, ev: EpisodeValidation) -> dict:
        def cv(v): return {"code":v.code,"result":v.result,
            "is_valid":v.is_valid,"is_acceptable_pdx":v.is_acceptable_pdx,
            "message":v.message,"query_template":v.query_template,
            "suggested_codes":v.suggested_codes,"mdc":v.mdc,
            "layer_reached":v.layer_reached}
        return {"pdx_validation":cv(ev.pdx_validation),
            "adx_validations":[cv(v) for v in ev.adx_validations],
            "achi_validations":[cv(v) for v in ev.achi_validations],
            "episode_valid":ev.episode_valid,
            "submission_ready":ev.submission_ready,
            "error_drg_risk":ev.error_drg_risk,
            "flags":ev.flags,"summary":ev.summary}
