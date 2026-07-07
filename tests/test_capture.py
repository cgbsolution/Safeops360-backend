"""Guided Field Capture — offline unit tests (no DB), house style of test_rca.py.

Covers the triage 5x5 banding, conversion maps, anonymous masking/ownership,
and the synthesised-description contract (>=10 chars for module conversion).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services import capture as svc


# ── 5x5 banding ───────────────────────────────────────────────────────────────
def test_risk_level_banding_matches_std_5x5():
    assert svc.risk_level_for(1) == "LOW"
    assert svc.risk_level_for(4) == "LOW"
    assert svc.risk_level_for(5) == "MODERATE"
    assert svc.risk_level_for(9) == "MODERATE"
    assert svc.risk_level_for(10) == "HIGH"
    assert svc.risk_level_for(16) == "HIGH"
    assert svc.risk_level_for(17) == "CRITICAL"
    assert svc.risk_level_for(25) == "CRITICAL"


def test_severity_maps_are_total():
    # every self-severity and every risk band must map to a module severity
    for s in ("low", "medium", "high"):
        assert svc.SELF_SEVERITY_TO_MODULE[s] in ("LOW", "MEDIUM", "HIGH")
    for band in ("LOW", "MODERATE", "HIGH", "CRITICAL"):
        assert svc.RISK_LEVEL_TO_MODULE[band] in ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def test_hazard_map_targets_are_valid_observation_categories():
    from app.models.observation import ObservationCategory

    valid = {c.value for c in ObservationCategory}
    for target in svc.HAZARD_TO_OBS_CATEGORY.values():
        assert target in valid, f"{target} is not an ObservationCategory"


# ── anonymity ─────────────────────────────────────────────────────────────────
def test_anon_hash_is_deterministic_and_user_specific():
    a1 = svc.anon_hash("user-a")
    assert a1 == svc.anon_hash("user-a")
    assert a1 != svc.anon_hash("user-b")
    assert len(a1) == 64  # sha256 hex


def _sub(**overrides):
    base = dict(
        reporterId=None,
        isAnonymous=True,
        anonHash=svc.anon_hash("tech-1"),
        number="FLD-2026-NW-0001",
        categorySnapshot={"l1": {"code": "machine_guarding", "labels": {"en": "Machine guarding"}}},
        description=None,
        transcriptEnglish=None,
        transcriptOriginal=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_owner_of_anonymous_report_is_recognised_via_hash():
    sub = _sub()
    assert svc.is_owner(sub, "tech-1") is True
    assert svc.is_owner(sub, "tech-2") is False


def test_named_report_ownership_uses_reporter_id():
    sub = _sub(isAnonymous=False, anonHash=None, reporterId="tech-9")
    assert svc.is_owner(sub, "tech-9") is True
    assert svc.is_owner(sub, "tech-1") is False


def test_submission_out_masks_anonymous_reporter():
    reporter = SimpleNamespace(id="tech-1", name="Ramesh Kumar", designation="Operator")
    sub = SimpleNamespace(
        id="s1", number="FLD-2026-NW-0001", clientSubmissionId="c1", type="observation",
        status="submitted", isAnonymous=True, reporter=reporter, reporterId=None,
        plantId="p1", areaId=None, mapPinX=None, mapPinY=None, equipmentId=None,
        qrScanned=False, categoryL1Id=None, categoryL2Id=None, categorySnapshot=None,
        aiSuggested=False, aiConfidence=None, severitySelfReported="high",
        description=None, voiceLangCode=None, transcriptOriginal=None,
        transcriptEnglish=None, transcriptionStatus="none", triagedById=None,
        triagedAt=None, hiraLikelihood=None, hiraSeverity=None, riskScore=None,
        riskLevel=None, triageNote=None, convertedEntityType=None,
        convertedEntityId=None, convertedAt=None, linkedRcaIds=[], linkedCapaIds=[],
        linkedPtwIds=[], tapCount=6, durationMs=42000, wasOffline=False,
        appVersion=None, deviceLang="hi", createdAtClient=None, createdAt=None,
    )
    masked = svc.submission_out(sub)
    assert masked["reporter"] is None  # non-owner never sees identity
    owner_view = svc.submission_out(sub, viewer_is_owner=True)
    assert owner_view["reporter"]["name"] == "Ramesh Kumar"
    unmasked = svc.submission_out(sub, unmasked=True)
    assert unmasked["reporter"]["id"] == "tech-1"
    # tap-count instrumentation must survive serialisation (spec 1.1.7)
    assert masked["capture"]["tapCount"] == 6


# ── conversion narrative ──────────────────────────────────────────────────────
def test_synth_description_is_long_enough_for_module_schemas():
    text = svc.synth_description(_sub())
    assert len(text) >= 10
    assert "Machine guarding" in text
    assert "FLD-2026-NW-0001" in text


def test_synth_description_prefers_english_transcript():
    sub = _sub(transcriptEnglish="Guard missing on cutting machine 3", transcriptOriginal="कटिंग मशीन 3 पर गार्ड नहीं है")
    text = svc.synth_description(sub)
    assert "Guard missing on cutting machine 3" in text
    assert "कटिंग" not in text  # English preferred when present


def test_synth_description_falls_back_to_original_transcript():
    sub = _sub(transcriptOriginal="कटिंग मशीन 3 पर गार्ड नहीं है")
    assert "कटिंग मशीन 3" in svc.synth_description(sub)
