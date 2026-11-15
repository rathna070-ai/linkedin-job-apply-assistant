"""Structured-output schemas for Claude responses."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class PostAnalysis(BaseModel):
    post_index: int
    is_hiring_post: bool
    company: str
    role: str
    location: str
    experience_required: str
    salary: str
    contact_email: str
    job_description: str
    match_score: int
    match_reasons: str
    gaps: str


class PostAnalysisBatch(BaseModel):
    results: list[PostAnalysis]


class EmailDraft(BaseModel):
    subject: str
    body: str


class Competency(BaseModel):
    label: str
    items: str


class Role(BaseModel):
    title: str
    dates: str
    company: str
    context: str
    bullets: list[str]


class Project(BaseModel):
    name: str
    description: str


class TailoredResume(BaseModel):
    full_name: str
    headline: str
    contact_line: str
    links_line: str
    summary: str
    highlights: list[str]
    competencies: list[Competency]
    experience: list[Role]
    projects: list[Project]
    projects_tech: str
    certifications: list[str]
    awards: list[str]
    education: list[str]
    extra_line: str
    jd_keywords_used: list[str]
    gaps: list[str]


class ReplyAssessment(BaseModel):
    category: Literal["interview", "request_info", "rejected", "auto_reply", "replied", "other"]
    summary: str
