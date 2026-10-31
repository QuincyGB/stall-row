"""The Gradeability Self-Test Report link: every discovery surface
names this stall's own report at /.well-known/gradeability-report.json
(committed at app/data/gradeability-report.json, served verbatim).
"""

from app.config import load_settings
from app.discovery import (
    GRADEABILITY_REPORT_PATH,
    agent_card,
    gradeability_extension,
    llms_txt,
    skill_md,
    x402_manifest,
)

BASE = "https://gradeability.example"
REPORT = BASE + "/.well-known/gradeability-report.json"


def test_gradeability_reference_is_the_stalls_own_report():
    assert GRADEABILITY_REPORT_PATH == "/.well-known/gradeability-report.json"
    assert gradeability_extension(BASE)["report"] == REPORT


def test_manifest_and_card_link_the_gradeability_report():
    settings = load_settings()
    manifest = x402_manifest(settings, BASE)
    assert manifest["extensions"]["gradeability"]["report"] == REPORT
    card = agent_card(settings, BASE)
    assert card["gradeability"]["report"] == REPORT


def test_skill_and_llms_name_the_gradeability_report():
    settings = load_settings()
    for doc in (skill_md(settings, BASE), llms_txt(settings, BASE)):
        assert REPORT in doc
