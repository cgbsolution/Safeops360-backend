"""P2-9 — Audit report PDF generation (fpdf2; pure-Python, no system deps).

Renders an AuditReport's immutable snapshot to a branded A4 PDF: cover page,
INTERIM 'PROVISIONAL' watermark on every page, category compliance, findings
register, CAPA summary, sign-off block (FINAL), page numbers + confidential footer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fpdf import FPDF

_REPL = {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
         "•": "*", "₹": "Rs ", "→": "->", "≥": ">=", "≤": "<=", " ": " "}


def _s(text: Any) -> str:
    """Sanitise to latin-1 (fpdf2 core fonts) — map common Unicode then drop the rest."""
    t = str(text)
    for k, v in _REPL.items():
        t = t.replace(k, v)
    return t.encode("latin-1", "replace").decode("latin-1")


NAVY = (30, 41, 90)
PURPLE = (88, 28, 135)
GREY = (100, 100, 100)
LIGHT = (235, 235, 240)
RED = (192, 57, 43)
AMBER = (230, 126, 34)
GREEN = (39, 139, 87)


def _rag(pct: float | None) -> tuple[int, int, int]:
    if pct is None:
        return GREY
    return GREEN if pct >= 85 else (AMBER if pct >= 70 else RED)


class _Report(FPDF):
    def __init__(self, report_type: str, audit_code: str, snapshot_hash: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.report_type = (report_type or "").upper()
        self.audit_code = audit_code
        self.snapshot_hash = snapshot_hash
        self.set_auto_page_break(auto=True, margin=20)
        self.set_title(_s(f"Audit Report {audit_code}"))

    # Centralised sanitisation — fpdf2 core fonts are latin-1 only.
    def cell(self, *a, **k):  # type: ignore[override]
        if len(a) >= 3 and isinstance(a[2], str):
            a = (a[0], a[1], _s(a[2])) + a[3:]
        for key in ("txt", "text"):
            if key in k and isinstance(k[key], str):
                k[key] = _s(k[key])
        return super().cell(*a, **k)

    def multi_cell(self, *a, **k):  # type: ignore[override]
        if len(a) >= 3 and isinstance(a[2], str):
            a = (a[0], a[1], _s(a[2])) + a[3:]
        for key in ("txt", "text"):
            if key in k and isinstance(k[key], str):
                k[key] = _s(k[key])
        return super().multi_cell(*a, **k)

    def text(self, x, y, txt=""):  # type: ignore[override]
        return super().text(x, y, _s(txt))

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*NAVY)
        self.cell(0, 8, f"SafeOps360 — Audit Report {self.audit_code}", border=0, ln=0, align="L")
        self.cell(0, 8, self.report_type, border=0, ln=1, align="R")
        self.set_draw_color(*LIGHT)
        self.line(10, 18, 200, 18)
        self.ln(4)
        if self.report_type == "INTERIM":
            self._watermark()

    def _watermark(self):
        self.set_text_color(230, 210, 210)
        self.set_font("Helvetica", "B", 50)
        with self.rotation(45, x=105, y=150):
            self.text(55, 150, "PROVISIONAL")
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*GREY)
        self.cell(0, 6, "CONFIDENTIAL", border=0, ln=0, align="L")
        self.cell(0, 6, f"Page {self.page_no()} of {{nb}}", border=0, ln=0, align="C")
        self.cell(0, 6, f"hash {self.snapshot_hash[:12]}", border=0, ln=1, align="R")


def _h(pdf: _Report, text: str):
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*PURPLE)
    pdf.cell(0, 8, text, border=0, ln=1)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 10)


def render_audit_report_pdf(report: dict[str, Any], generated_by_name: str = "—") -> bytes:
    snap: dict[str, Any] = report.get("snapshot") or {}
    rtype = report.get("reportType") or snap.get("reportType") or "INTERIM"
    code = snap.get("auditCode") or report.get("reportCode") or "—"
    pdf = _Report(rtype, code, report.get("id", ""))
    pdf.alias_nb_pages()

    # ── Cover page ──
    pdf.add_page()
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 210, 45, style="F")
    pdf.set_y(14)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 12, "SafeOps360", border=0, ln=1, align="C")
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 7, "Audit & Compliance Report", border=0, ln=1, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(20)
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 9, snap.get("title") or "Audit Report", align="C")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 13)
    badge = RED if rtype.upper() == "INTERIM" else GREEN
    pdf.set_text_color(*badge)
    pdf.cell(0, 8, f"{rtype.upper()} REPORT" + (" — PROVISIONAL, SUBJECT TO CHANGE" if rtype.upper() == "INTERIM" else ""), border=0, ln=1, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 11)
    now = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    for label, val in [
        ("Audit Code", code), ("Audit Type", snap.get("auditType") or "—"),
        ("Site", snap.get("siteId") or "—"), ("Planned", snap.get("plannedDate") or "—"),
        ("Closed", snap.get("closedAt") or "—"), ("Generated", now), ("Generated by", generated_by_name),
    ]:
        pdf.cell(50, 7, f"{label}:", border=0, ln=0)
        pdf.cell(0, 7, str(val)[:80], border=0, ln=1)
    pdf.ln(4)
    pct = snap.get("overallScorePct")
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*_rag(pct))
    pdf.cell(0, 10, f"Overall compliance: {pct if pct is not None else '—'}%   ({snap.get('overallResult') or '—'})", border=0, ln=1)
    pdf.set_text_color(0, 0, 0)

    # ── Executive summary ──
    pdf.add_page()
    _h(pdf, "1. Executive Summary")
    pdf.multi_cell(0, 6, (
        f"Checkpoints assessed: {snap.get('checkpointsAssessed', 0)} of {snap.get('checkpointsTotal', 0)}. "
        f"Pass {snap.get('passCount', 0)}, Fail {snap.get('failCount', 0)}, Partial {snap.get('partialCount', 0)}, N/A {snap.get('naCount', 0)}. "
        f"Failures by severity — Critical {snap.get('criticalFailures', 0)}, Major {snap.get('majorFailures', 0)}, Minor {snap.get('minorFailures', 0)}. "
        f"Open iterations {snap.get('openIterationsCount', 0)} ({snap.get('criticalOpenCount', 0)} critical)."
    ))
    pdf.ln(3)

    # ── Category compliance (RAG) ──
    _h(pdf, "2. Category-wise Compliance")
    cats = snap.get("categoryScores") or {}
    if isinstance(cats, dict):
        cats = [{"category": k, **(v if isinstance(v, dict) else {"scorePct": v})} for k, v in cats.items()]
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(*LIGHT)
    pdf.cell(140, 7, "Category", border=1, ln=0, fill=True)
    pdf.cell(40, 7, "Score %", border=1, ln=1, fill=True, align="C")
    pdf.set_font("Helvetica", "", 9)
    for c in (cats or [])[:30]:
        name = str(c.get("category") or c.get("name") or "-")[:60]
        sc = c.get("scorePct", c.get("score"))
        pdf.cell(140, 6, name, border=1, ln=0)
        pdf.set_text_color(*_rag(sc if isinstance(sc, (int, float)) else None))
        pdf.cell(40, 6, f"{sc if sc is not None else '-'}", border=1, ln=1, align="C")
        pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    # ── Findings register ──
    _h(pdf, "3. Findings Register")
    findings = snap.get("findings") or []
    if not findings:
        pdf.cell(0, 6, "No findings recorded.", border=0, ln=1)
    else:
        # One finding per block: severity + clause header line, then the finding
        # text as a full-width line (robust against long unbreakable tokens).
        for f in findings[:60]:
            sev = str(f.get("severity") or "-")
            clause = str(f.get("standardClauseRef") or f.get("clause") or "-")
            pdf.set_x(10)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*(RED if "CRIT" in sev or "MAJOR" in sev else GREY))
            pdf.cell(0, 5, f"[{sev[:16]}]  {clause[:24]}", border=0, ln=1)
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_x(10)
            pdf.multi_cell(190, 5, str(f.get("title") or f.get("description") or "-")[:160], border=0)
            pdf.ln(1)
    pdf.ln(3)

    # ── CAPA summary ──
    _h(pdf, "4. CAPA Summary")
    cs = snap.get("capaSummary") or {}
    pdf.cell(0, 6, f"Total CAPAs: {cs.get('total', 0)}   Open: {cs.get('open', 0)}   Overdue: {cs.get('overdue', 0)}", border=0, ln=1)
    pdf.ln(3)

    # ── Sign-off (FINAL) ──
    if rtype.upper() == "FINAL":
        _h(pdf, "5. Sign-Off")
        signs = report.get("signOffs") or []
        if not signs:
            pdf.cell(0, 6, "Awaiting sign-off.", border=0, ln=1)
        for s in signs:
            pdf.cell(0, 6, f"{s.get('role', '—')}: {s.get('name', '—')}  —  {s.get('signedAt', '')}", border=0, ln=1)

    out = pdf.output()
    return bytes(out)
