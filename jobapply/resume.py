"""Resume text extraction and rendering of tailored resumes to DOCX and PDF."""

from __future__ import annotations

import io
import re
from xml.sax.saxutils import escape

from .schemas import TailoredResume


# ----- extraction -----
def extract_docx_text(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    lines: list[str] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style is not None else ""
        if "list" in style:
            text = f"- {text}"
        lines.append(text)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append(" | ".join(dict.fromkeys(cells)))
    return "\n".join(lines)


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def safe_filename(*parts: str) -> str:
    joined = "_".join(p for p in parts if p)
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", joined).strip("_")
    return cleaned[:120] or "resume"


# ----- DOCX rendering -----
def render_docx(resume: TailoredResume) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.27)
    section.page_height = Inches(11.69)
    for side in ("left_margin", "right_margin"):
        setattr(section, side, Inches(0.6))
    section.top_margin = Inches(0.5)
    section.bottom_margin = Inches(0.5)
    text_width = section.page_width - section.left_margin - section.right_margin

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(2)

    def para(text: str = "", *, bold=False, size=None, align=None, color=None, space_after=2):
        p = doc.add_paragraph()
        if text:
            run = p.add_run(text)
            run.bold = bold
            if size:
                run.font.size = Pt(size)
            if color:
                run.font.color.rgb = RGBColor(*color)
        if align is not None:
            p.alignment = align
        p.paragraph_format.space_after = Pt(space_after)
        return p

    def heading(text: str):
        p = para(text.upper(), bold=True, size=11.5, color=(0x1F, 0x3A, 0x5F), space_after=3)
        p.paragraph_format.space_before = Pt(8)
        p_pr = p._p.get_or_add_pPr()
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")
        bottom.set(qn("w:space"), "1")
        bottom.set(qn("w:color"), "1F3A5F")
        border.append(bottom)
        p_pr.append(border)

    def bullet(text: str, label: str | None = None):
        p = doc.add_paragraph(style="List Bullet")
        if label:
            p.add_run(f"{label}: ").bold = True
        p.add_run(text)
        p.paragraph_format.space_after = Pt(1)

    para(resume.full_name.upper(), bold=True, size=18, align=WD_ALIGN_PARAGRAPH.CENTER, color=(0x1F, 0x3A, 0x5F))
    if resume.headline:
        para(resume.headline, bold=True, size=11, align=WD_ALIGN_PARAGRAPH.CENTER)
    for line in (resume.contact_line, resume.links_line):
        if line:
            para(line, size=9.5, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=1)

    if resume.summary:
        heading("Professional Summary")
        para(resume.summary)
    if resume.highlights:
        heading("Career Highlights")
        for item in resume.highlights:
            bullet(item)
    if resume.competencies:
        heading("Core Competencies")
        for comp in resume.competencies:
            bullet(comp.items, comp.label)
    if resume.experience:
        heading("Professional Experience")
        for role in resume.experience:
            p = para(space_after=0)
            p.paragraph_format.space_before = Pt(5)
            p.paragraph_format.tab_stops.add_tab_stop(text_width, WD_TAB_ALIGNMENT.RIGHT)
            p.add_run(role.title).bold = True
            if role.dates:
                p.add_run(f"\t{role.dates}").bold = True
            if role.company:
                r = para(space_after=0).add_run(role.company)
                r.italic = True
            if role.context:
                para(role.context, size=9.5, space_after=1)
            for b in role.bullets:
                bullet(b)
    if resume.projects:
        heading("Projects")
        for proj in resume.projects:
            bullet(proj.description, proj.name)
        if resume.projects_tech:
            para(f"Tech: {resume.projects_tech}", size=9.5)
    if resume.certifications:
        heading("Certifications")
        for c in resume.certifications:
            bullet(c)
    if resume.awards:
        heading("Awards & Recognition")
        for a in resume.awards:
            bullet(a)
    if resume.education:
        heading("Education")
        for e in resume.education:
            para(e)
    if resume.extra_line:
        para(resume.extra_line)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ----- PDF rendering -----
_PUNCT = {
    "→": "->",
    "←": "<-",
    "≥": ">=",
    "≤": "<=",
    " ": " ",
    "‑": "-",
    "−": "-",
    "≈": "~",
}


def _pdf_text(text: str) -> str:
    """Escapes markup and keeps only characters the built-in PDF fonts can draw."""
    for src, dst in _PUNCT.items():
        text = text.replace(src, dst)
    text = text.encode("cp1252", "replace").decode("cp1252")
    return escape(text)


def render_pdf(resume: TailoredResume) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    navy = colors.HexColor("#1F3A5F")
    base = getSampleStyleSheet()["Normal"]
    body = ParagraphStyle("body", parent=base, fontName="Helvetica", fontSize=9.6, leading=12.4)
    small = ParagraphStyle("small", parent=body, fontSize=9, leading=11.5)
    center = ParagraphStyle("center", parent=small, alignment=TA_CENTER)
    name_style = ParagraphStyle(
        "name", parent=body, fontName="Helvetica-Bold", fontSize=17, leading=21, alignment=TA_CENTER, textColor=navy
    )
    headline_style = ParagraphStyle("headline", parent=body, fontName="Helvetica-Bold", fontSize=10.5, alignment=TA_CENTER)
    head_style = ParagraphStyle(
        "head", parent=body, fontName="Helvetica-Bold", fontSize=10.8, textColor=navy, spaceBefore=7, spaceAfter=1
    )
    bullet_style = ParagraphStyle("bullet", parent=body, leftIndent=11, bulletIndent=2, spaceAfter=1)
    dates_style = ParagraphStyle("dates", parent=body, alignment=TA_RIGHT)

    story: list = []

    def heading(text: str):
        story.append(Paragraph(_pdf_text(text.upper()), head_style))
        story.append(HRFlowable(width="100%", thickness=0.7, color=navy, spaceBefore=1, spaceAfter=3))

    def bullet(text: str, label: str | None = None):
        markup = (f"<b>{_pdf_text(label)}:</b> " if label else "") + _pdf_text(text)
        story.append(Paragraph(markup, bullet_style, bulletText="•"))

    story.append(Paragraph(_pdf_text(resume.full_name.upper()), name_style))
    if resume.headline:
        story.append(Paragraph(_pdf_text(resume.headline), headline_style))
    for line in (resume.contact_line, resume.links_line):
        if line:
            story.append(Paragraph(_pdf_text(line), center))
    story.append(Spacer(1, 3))

    if resume.summary:
        heading("Professional Summary")
        story.append(Paragraph(_pdf_text(resume.summary), body))
    if resume.highlights:
        heading("Career Highlights")
        for item in resume.highlights:
            bullet(item)
    if resume.competencies:
        heading("Core Competencies")
        for comp in resume.competencies:
            bullet(comp.items, comp.label)
    if resume.experience:
        heading("Professional Experience")
        width = A4[0] - 30 * mm
        for role in resume.experience:
            row = Table(
                [[Paragraph(f"<b>{_pdf_text(role.title)}</b>", body), Paragraph(f"<b>{_pdf_text(role.dates)}</b>", dates_style)]],
                colWidths=[width * 0.72, width * 0.28],
            )
            row.setStyle(
                TableStyle(
                    [
                        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ]
                )
            )
            row.hAlign = "LEFT"
            story.append(row)
            if role.company:
                story.append(Paragraph(f"<i>{_pdf_text(role.company)}</i>", body))
            if role.context:
                story.append(Paragraph(_pdf_text(role.context), small))
            for b in role.bullets:
                bullet(b)
    if resume.projects:
        heading("Projects")
        for proj in resume.projects:
            bullet(proj.description, proj.name)
        if resume.projects_tech:
            story.append(Paragraph(f"<b>Tech:</b> {_pdf_text(resume.projects_tech)}", small))
    if resume.certifications:
        heading("Certifications")
        for c in resume.certifications:
            bullet(c)
    if resume.awards:
        heading("Awards & Recognition")
        for a in resume.awards:
            bullet(a)
    if resume.education:
        heading("Education")
        for e in resume.education:
            story.append(Paragraph(_pdf_text(e), body))
    if resume.extra_line:
        story.append(Spacer(1, 3))
        story.append(Paragraph(_pdf_text(resume.extra_line), body))

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"{resume.full_name} - Resume",
        author=resume.full_name,
    )
    doc.build(story)
    return out.getvalue()
