"""The Inspector Quickstart Pack link: every discovery surface names
the pack's canonical pages on StallRow (curl / Python / TypeScript).
The pages are hosted on StallRow only; the other stalls link the
same canonical URLs.
"""

from app.config import load_settings
from app.discovery import QUICKSTART, agent_card, llms_txt, skill_md, x402_manifest

BASE = "https://stall-row.example"
CURL = "https://stall-row-production.up.railway.app/quickstart/curl"
PYTHON = "https://stall-row-production.up.railway.app/quickstart/python"
TYPESCRIPT = "https://stall-row-production.up.railway.app/quickstart/typescript"


def test_quickstart_reference_is_the_canonical_pages():
    assert QUICKSTART["curl"] == CURL
    assert QUICKSTART["python"] == PYTHON
    assert QUICKSTART["typescript"] == TYPESCRIPT


def test_manifest_and_card_link_the_quickstart():
    settings = load_settings()
    manifest = x402_manifest(settings, BASE)
    assert manifest["extensions"]["quickstart"]["curl"] == CURL
    assert manifest["extensions"]["quickstart"]["typescript"] == TYPESCRIPT
    card = agent_card(settings, BASE)
    assert card["quickstart"]["python"] == PYTHON
    assert card["quickstart"]["curl"] == CURL


def test_skill_and_llms_name_the_quickstart():
    settings = load_settings()
    for doc in (skill_md(settings, BASE), llms_txt(settings, BASE)):
        assert CURL in doc
        assert PYTHON in doc
        assert TYPESCRIPT in doc
