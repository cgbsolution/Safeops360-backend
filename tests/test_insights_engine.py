"""AI Insights engine — offline unit tests (house no-DB style).

Covers the pure layers: template slot-filling, confidence gating, keyword
tokenisation, the 15-min cache, and the engine's suppression + ranking gates
(driven through an injected fake rule so no DB is touched). The DB-bound rule
functions (compute_incident / compute_nearmiss) are exercised against the live
flow, not here.
"""

from __future__ import annotations

import pytest

from app.schemas.insights import Insight, Signal
from app.services.insights import cache, engine
from app.services.insights.common import confidence_for, keywords
from app.services.insights.templates import fill


# ─── templates: phrasing is fully slot-filled, never half-filled ────────────

def test_fill_produces_grounded_headline():
    text = fill(
        "incident.cluster.rootcause", count=3, total=4, keyword="confined", plant="Meridian North"
    )
    assert "3 of 4" in text and "confined" in text and "Meridian North" in text


def test_fill_missing_slot_raises():
    with pytest.raises(KeyError):
        fill("incident.cluster.rootcause", count=3)  # missing total/keyword/plant


def test_fill_unknown_template_raises():
    with pytest.raises(KeyError):
        fill("no.such.template")


def test_headline_backstop_trims_to_90():
    text = fill(
        "incident.cluster.rootcause",
        count=999,
        total=999,
        keyword="x" * 200,
        plant="y" * 200,
    )
    assert len(text) <= 90


# ─── confidence gating is a function of sample size ─────────────────────────

def test_confidence_thresholds():
    assert confidence_for(1) == "low"
    assert confidence_for(4) == "low"
    assert confidence_for(5) == "medium"
    assert confidence_for(14) == "medium"
    assert confidence_for(15) == "high"


# ─── keyword tokenisation drops stopwords/noise ─────────────────────────────

def test_keywords_drops_stopwords_and_short_tokens():
    toks = keywords(["Worker slipped near the confined space access"], "confined valve")
    assert "confined" in toks
    assert "the" not in toks and "near" not in toks
    assert "worker" not in toks  # domain stopword


# ─── cache: 15-min TTL get/put ──────────────────────────────────────────────

def test_cache_roundtrip_and_expiry(monkeypatch):
    cache.clear()
    k = cache.make_key("incident", "P1", {"from": None, "to": None})
    assert cache.get(k) is None
    cache.put(k, {"v": 1})
    assert cache.get(k) == {"v": 1}
    # force expiry
    cache._store[k] = (0.0, {"v": 1})
    assert cache.get(k) is None


def test_make_key_is_scope_sensitive():
    a = cache.make_key("incident", "P1", {"from": None, "to": None})
    b = cache.make_key("incident", "P2", {"from": None, "to": None})
    assert a != b


# ─── engine gates: suppression, ranking, cache — via an injected fake rule ──

def _insight(id_: str, sev: str, conf: str) -> Insight:
    return Insight(
        id=id_, kind="cluster", severity=sev, headline="h", evidence="e",
        recordRefs=["A-1"], confidence=conf,
    )


def _install_fake(monkeypatch, bar, signals, count):
    async def fake(db, *, plant=None, date_from=None, date_to=None):  # noqa: ANN001
        return list(bar), list(signals), count

    monkeypatch.setitem(engine._RULES, "faketest", fake)
    cache.clear()


async def test_engine_suppresses_below_floor(monkeypatch):
    _install_fake(monkeypatch, [_insight("x", "high", "high")],
                  [Signal(recordId="1", recordRef="A-1", kind="cluster", severity="high", label="L", evidence="e")], 4)
    resp = await engine.compute(None, "faketest")
    assert resp.suppressed is True
    assert resp.bar == [] and resp.signals == []
    assert resp.reason == "insufficient_records"


async def test_engine_ranks_and_keeps_low_confidence_last(monkeypatch):
    bar = [
        _insight("info-hi", "info", "high"),
        _insight("crit-lo", "critical", "low"),
        _insight("high-med", "high", "medium"),
        _insight("watch-hi", "watch", "high"),
    ]
    _install_fake(monkeypatch, bar, [], 20)
    resp = await engine.compute(None, "faketest")
    assert len(resp.bar) == 3  # trimmed to 3
    # critical outranks all despite low confidence (not hidden, ranked first)
    assert resp.bar[0].id == "crit-lo"
    assert resp.bar[1].id == "high-med"
    assert resp.suppressed is False


async def test_engine_serves_from_cache(monkeypatch):
    _install_fake(monkeypatch, [_insight("x", "high", "high")], [], 20)
    first = await engine.compute(None, "faketest")
    second = await engine.compute(None, "faketest")
    assert first.cached is False
    assert second.cached is True


async def test_engine_unknown_module_raises(monkeypatch):
    with pytest.raises(KeyError):
        await engine.compute(None, "definitely-not-a-module")


# ─── all eight list screens are wired into the engine ───────────────────────

def test_all_eight_modules_registered():
    assert set(engine.SUPPORTED_MODULES) == {
        "incident", "nearmiss", "observation", "hira",
        "eai", "combined-risk", "capa", "moc",
    }


# ─── every new module's headline/evidence template fills, headline ≤ 90 ─────

# Representative slots per headline template — the values a rule computes.
_NEW_HEADLINES = {
    "observation.cluster.category": dict(count=6, total=8, plant="Meridian North Integrated Unit", category="Electrical"),
    "observation.duplicate": dict(groups=2, records=5),
    "hira.review.soon": dict(count=3, soonest_ref="HIRA-NW-007", days=12),
    "hira.review.overdue": dict(count=3, soonest_ref="HIRA-NW-007", days=12),
    "hira.cluster.hazard": dict(category="Confined Space Entry", plants=2),
    "eai.obligation.due": dict(obligations=4, studies=2),
    "eai.significance.count": dict(studies=3),
    "combined.reduced_no_capa": dict(count=5),
    "combined.area_cluster": dict(count=4, area="Finishing & Packing"),
    "capa.overdue": dict(count=8, worst_ref="RTM-NW-005", worst_days=44, severity="Critical"),
    "capa.backlog": dict(closed=0, opened=22),
    "capa.bottleneck": dict(owner="Ravi Menon", count=3),
    "moc.overdue": dict(count=4, worst_ref="MOC-NW-012", worst_days=30),
    "moc.cluster.critical": dict(count=2),
}


@pytest.mark.parametrize("key,slots", list(_NEW_HEADLINES.items()))
def test_new_module_headlines_fill_within_90(key, slots):
    text = fill(key, **slots)
    assert text and len(text) <= 90


# Signal labels must fit the Signal.label max_length (24).
_NEW_LABELS = [
    "signal.duplicate.label", "signal.severity_mismatch.label", "signal.escalate.label",
    "signal.unmitigated_critical.label", "signal.nudge_lead.label", "signal.monitoring_overdue.label",
    "signal.significant_aspect.label", "signal.not_active.label", "signal.audit_finding.label",
    "signal.stalled_draft.label",
]


@pytest.mark.parametrize("key", _NEW_LABELS)
def test_new_signal_labels_within_24(key):
    label = fill(key)
    assert label and len(label) <= 24
