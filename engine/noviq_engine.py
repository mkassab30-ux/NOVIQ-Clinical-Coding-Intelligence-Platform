"""
NOVIQ Engine — Pipeline Orchestrator
======================================
Phase 4, Deliverable 4.2

Single entry point for the complete NOVIQ Engine pipeline:
  EHR JSON-In → ACS scoring → Validation → AR-DRG Grouper → CodingSuggestion

Usage:
    engine     = NOVIQEngine()
    suggestion = engine.process_episode(episode_dict)
    suggestion.approve("DR-KASSAB")
    suggestion.assert_approved()      # gate before claim submission
    claim_dict = suggestion.to_dict()

FastAPI (3 lines):
    @app.post("/process")
    async def process(episode: dict) -> dict:
        return NOVIQEngine().process_episode(episode).to_dict()
"""

from __future__ import annotations

import warnings
from pathlib import Path

from models import (
    ACS_THRESHOLD_CODE,
    ACS_THRESHOLD_REVIEW,
    ACSScore,
    CodingSuggestion,
    EpisodeRecord,
)
from validation_rules import validate_episode
from grouper import ARDRGGrouper

DEFAULT_KB_PATH   = Path(__file__).parent / "ar_drg_kb_seed_v11_new_adrgs.json"
DEFAULT_EXCL_PATH = Path(__file__).parent / "dcl_exclusions.json"


# ---------------------------------------------------------------------------
# ACS Scoring Engine
# ---------------------------------------------------------------------------

class ACSScoring:
    """
    ACS Scoring Engine stub — integrates with Phase 0 ACS engine.

    ACS 0001 — Principal Diagnosis (max 7 pts):
      +3 confirmed by investigation
      +2 documented by physician
      +2 reason for admission
      ≥5 → code, 3-4 → physician review, <3 → do not code

    ACS 0002 — Additional Diagnoses (max 8 pts):
      +3 therapeutic treatment altered
      +3 diagnostic procedure ordered
      +2 increased clinical care
      ≥5 → code, 3-4 → physician review, <3 → do not code

    If episode.acs_pdx_score is pre-set by the caller (e.g. passed in from
    the EHR adapter after Phase 0 scoring), it is preserved as-is.
    """

    def score_episode(self, episode: EpisodeRecord) -> EpisodeRecord:
        """Score all diagnoses. Preserves pre-set scores."""
        if episode.acs_pdx_score == 0:
            episode.acs_pdx_score = self._score_pdx(episode.pdx)

        scored = {s.get("code") for s in episode.acs_adx_scores}
        for code in episode.adx:
            if code not in scored:
                score = self._score_adx(code)
                episode.acs_adx_scores.append(
                    ACSScore.from_score(
                        code, score, is_pdx=False,
                        justification="ACS Scoring Engine stub — replace with Phase 0 engine"
                    ).to_dict()
                )
        return episode

    def _score_pdx(self, pdx: str) -> int:
        """
        Stub — returns 5 (meets coding threshold).
        Production: use Phase 0 ACS Scoring Engine with EHR document analysis.
        """
        return 5

    def _score_adx(self, code: str) -> int:
        """
        Stub — returns 2 (below coding threshold, conservative).
        Production: use Phase 0 ACS Scoring Engine.
        """
        return 2


# ---------------------------------------------------------------------------
# NOVIQEngine — full pipeline orchestrator
# ---------------------------------------------------------------------------

class NOVIQEngine:
    """
    NOVIQ Engine — full pipeline orchestrator. JSON-In / JSON-Out.

    Runs a PatientEpisode through all modules in sequence:
      1. EpisodeRecord parsed from input JSON
      2. ACS Scoring Engine — scores PDX and all ADX
      3. Validation Rules — DCL exclusions, upcoding risk detection
      4. AR-DRG Grouper — 5-step V11.0 grouper
      5. CodingSuggestion — output with full provenance + physician gate

    All dependencies are injected and swappable (testing, V12.0 upgrade).
    """

    def __init__(
        self,
        kb_path:     Path = DEFAULT_KB_PATH,
        excl_path:   Path = DEFAULT_EXCL_PATH,
        acs_engine:  ACSScoring | None = None,
        grouper:     ARDRGGrouper | None = None,
    ) -> None:
        self.acs_engine = acs_engine or ACSScoring()
        self._excl_path = excl_path
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.grouper = grouper or ARDRGGrouper(kb_path, excl_path)

    # ------------------------------------------------------------------
    # Primary entry point
    # ------------------------------------------------------------------

    def process_episode(self, episode_input: dict) -> CodingSuggestion:
        """
        PRIMARY ENTRY POINT — JSON-In / CodingSuggestion out.

        Args:
            episode_input: PatientEpisode dict

        Returns:
            CodingSuggestion with approval_status=PENDING.

        Raises:
            KnowledgeBaseIncompleteError — if a required KB value is null (F25 threshold)
            PermissionError — if assert_approved() called before physician approves
        """
        # Step 1: Parse
        episode = EpisodeRecord.from_dict(episode_input)

        # Step 2: ACS Scoring
        episode = self.acs_engine.score_episode(episode)

        # Step 3: Validation — DCL exclusions + upcoding risk
        validation_result = validate_episode(
            episode.to_dict(),
            kb_path=self._excl_path,
        )

        # Step 4: AR-DRG Grouper
        grouper_result = self.grouper.group_episode(
            episode.to_grouper_input()
        )

        # Step 5: Build CodingSuggestion with full provenance
        suggestion = CodingSuggestion.from_pipeline_results(
            episode           = episode,
            grouper_result    = grouper_result,
            validation_result = validation_result,
        )

        return suggestion

    def process_episode_dict(self, episode_input: dict) -> dict:
        """
        Returns to_dict() directly — use for FastAPI endpoints.
        Note: approval_status will be PENDING — physician must approve separately.
        """
        return self.process_episode(episode_input).to_dict()


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

def process(episode_input: dict, **kwargs) -> CodingSuggestion:
    """One-line pipeline call for scripting and testing."""
    return NOVIQEngine(**kwargs).process_episode(episode_input)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    engine = NOVIQEngine()

    TEST_EPISODE = {
        "episode_id":    "NOVIQ-E001",
        "patient_age":   58,
        "patient_sex":   "Female",
        "admission_weight": None,
        "same_day":      False,
        "separation_mode": "discharge_home",
        "los_days":      12,
        "pdx":           "C48.1",
        "adx":           ["E11.9", "E61.1", "Z59.0"],
        "achi_codes":    ["96211-00"],
        "hours_mech_vent": None,
        "care_type":     "01",
        "ehr_documents": [
            "Initial Medical Report",
            "Admission Report",
            "Progress Notes",
            "Operation Notes",
            "Nursing Notes",
            "Discharge Summary"
        ],
        "acs_pdx_score": 6,  # pre-scored by ACS engine
    }

    print("=" * 60)
    print("NOVIQ Engine — full pipeline smoke test")
    print("=" * 60)

    suggestion = engine.process_episode(TEST_EPISODE)
    d = suggestion.to_dict()

    print(f"\nEpisode:          {d['episode_id']}")
    print(f"AR-DRG:           {d['proposed_codes']['ar_drg']}")
    print(f"Description:      {d['proposed_codes']['ar_drg_desc']}")
    print(f"Approval status:  {d['approval_status']}")
    print(f"ACS PDX score:    {d['acs_scores']['pdx_score']}/7")
    print(f"DCL excluded:     {d['provenance']['dcl_excluded_count']}")
    print(f"Upcoding risk:    {d['provenance']['upcoding_risk_count']}")
    print(f"ACHI trigger:     {d['provenance']['achi_trigger']}")
    print(f"Justification:    {d['acs_scores']['coding_justification']}")
    if d["flags"]:
        print(f"Flags:")
        for f in d["flags"]:
            print(f"  - {f}")

    print(f"\n--- Physician approval gate ---")
    try:
        suggestion.assert_approved()
    except PermissionError as e:
        print(f"Gate fired correctly: {str(e)[:80]}...")

    suggestion.approve("DR-KASSAB-001")
    suggestion.assert_approved()  # now passes
    print(f"Approved by:  {suggestion.approved_by}")
    print(f"Status:       {suggestion.approval_status}")
    print(f"\n✓ Full pipeline complete")
