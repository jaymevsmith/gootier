"""Guard: customer-visible templates must not name the old company or domain."""
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def test_templates_have_no_old_company_copy():
    for p in TEMPLATES.glob("*.html"):
        text = p.read_text().lower()
        assert "jhomeautomation" not in text, p.name
        assert "jhome automation" not in text, p.name
        assert "jhome tokens" not in text, p.name
