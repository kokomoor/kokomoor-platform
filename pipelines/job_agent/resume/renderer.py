"""Render a ``TailoredResumeDocument`` to a .docx file.

Uses python-docx to build a clean, one-page-friendly resume with
consistent Calibri typography, tight margins, and professional layout.
No external template file required — styling is defined in code for
full determinism and testability.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used at runtime
from typing import Any

import structlog
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from pipelines.job_agent.models.resume_tailoring import (
    TailoredResumeDocument,  # noqa: TC001 — used at runtime
)

logger = structlog.get_logger(__name__)

_FONT_NAME = "Calibri"


def render_resume_docx(doc: TailoredResumeDocument, output_path: Path) -> Path:
    """Build a .docx resume from *doc* and write it to *output_path*.

    Returns the resolved output path.
    """
    document = Document()

    # Tight margins for one-page fit.
    section = document.sections[0]
    section.top_margin = Inches(0.5)
    section.bottom_margin = Inches(0.5)
    section.left_margin = Inches(0.6)
    section.right_margin = Inches(0.6)

    # ── Header ──
    _add_centered(document, doc.name, bold=True, size=14, space_after=0)
    contact_parts = [p for p in [doc.location, doc.email, doc.phone, doc.linkedin, doc.github] if p]
    _add_centered(document, " | ".join(contact_parts), size=9, space_after=1)
    if doc.clearance:
        _add_centered(document, doc.clearance, bold=True, size=9, space_after=2)

    # ── Summary ──
    if doc.summary:
        _add_section_heading(document, "PROFESSIONAL SUMMARY")
        _add_body(document, doc.summary)

    # ── Experience ──
    if doc.experience:
        _add_section_heading(document, "EXPERIENCE")
        for exp in doc.experience:
            _add_entry_heading(document, f"{exp.company} — {exp.title}", exp.dates)
            for bullet in exp.bullets:
                _add_bullet(document, bullet.text)

    # ── Education ──
    if doc.education:
        _add_section_heading(document, "EDUCATION")
        for edu in doc.education:
            date_str = edu.graduation
            if edu.gpa:
                date_str += f" | GPA: {edu.gpa}"
            _add_entry_heading(document, f"{edu.school} — {edu.degree}", date_str)
            for bullet in edu.bullets:
                _add_bullet(document, bullet.text)

    # ── Skills ──
    if doc.skills_highlight:
        _add_section_heading(document, "TECHNICAL SKILLS")
        _add_body(document, ", ".join(doc.skills_highlight))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output_path))
    logger.info("resume_rendered", path=str(output_path))
    return output_path


# ── private formatting helpers ─────────────────────────────────────────


def _add_centered(
    document: Any,
    text: str,
    *,
    bold: bool = False,
    size: int = 10,
    space_after: int = 0,
) -> None:
    para = document.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para.paragraph_format.space_before = Pt(0)
    para.paragraph_format.space_after = Pt(space_after)
    run = para.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = _FONT_NAME


def _add_section_heading(document: Any, title: str) -> None:
    para = document.add_paragraph()
    para.paragraph_format.space_before = Pt(8)
    para.paragraph_format.space_after = Pt(2)
    run = para.add_run(title)
    run.bold = True
    run.font.size = Pt(11)
    run.font.name = _FONT_NAME
    _add_bottom_border(para)


def _add_entry_heading(document: Any, left_text: str, right_text: str) -> None:
    para = document.add_paragraph()
    para.paragraph_format.space_before = Pt(4)
    para.paragraph_format.space_after = Pt(0)
    left_run = para.add_run(left_text)
    left_run.bold = True
    left_run.font.size = Pt(10)
    left_run.font.name = _FONT_NAME
    if right_text:
        sep_run = para.add_run("  |  ")
        sep_run.font.size = Pt(10)
        sep_run.font.name = _FONT_NAME
        right_run = para.add_run(right_text)
        right_run.font.size = Pt(10)
        right_run.font.name = _FONT_NAME


def _add_bullet(document: Any, text: str) -> None:
    para = document.add_paragraph()
    para.paragraph_format.space_before = Pt(0)
    para.paragraph_format.space_after = Pt(0)
    para.paragraph_format.left_indent = Inches(0.25)
    run = para.add_run(f"\u2022  {text}")
    run.font.size = Pt(10)
    run.font.name = _FONT_NAME


def _add_body(document: Any, text: str) -> None:
    para = document.add_paragraph()
    para.paragraph_format.space_before = Pt(1)
    para.paragraph_format.space_after = Pt(1)
    run = para.add_run(text)
    run.font.size = Pt(10)
    run.font.name = _FONT_NAME


def _add_bottom_border(paragraph: Any) -> None:
    """Add a thin bottom border to a paragraph (section separator)."""
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "333333")
    p_bdr.append(bottom)
    p_pr.append(p_bdr)
