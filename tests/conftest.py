import sys
from pathlib import Path

import mongomock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobapply.schemas import Competency, Project, Role, TailoredResume  # noqa: E402
from jobapply.store import Store  # noqa: E402


@pytest.fixture
def store():
    return Store(db_name="test_db", client=mongomock.MongoClient(tz_aware=True))


@pytest.fixture
def tailored():
    return TailoredResume(
        full_name="Asha Rao",
        headline="Delivery Manager | Program Manager",
        contact_line="Bengaluru, India | asha@example.com | +91 90000 00000",
        links_line="linkedin.com/in/asha-example",
        summary="Delivery leader with 15 years in IT services – BFSI & manufacturing → cloud.",
        highlights=["Grew an account from $1M to $2M.", "Raised automation from 0% to 30%."],
        competencies=[Competency(label="Delivery", items="Governance, RAIDD, KPIs")],
        experience=[
            Role(
                title="Delivery Manager",
                dates="Apr 2022 – Present",
                company="Example Corp, Bengaluru",
                context="Client: Insurer | Team: 40",
                bullets=["Owned delivery for a 40-member team.", "Set up governance & KPIs <weekly>."],
            )
        ],
        projects=[Project(name="Defect RCA Analyzer", description="Classifies defects by root cause.")],
        projects_tech="Python, MongoDB",
        certifications=["SAFe 5.0 Agilist (2024)"],
        awards=["Rain Maker Award (2019)"],
        education=["Master's Degree, Computer Science | 2015"],
        extra_line="Languages: English, Tamil",
        jd_keywords_used=["governance"],
        gaps=["PMP"],
    )
