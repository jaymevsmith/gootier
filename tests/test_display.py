"""tests/test_display.py

Token counts render compactly (550, 2K, 750K, 20M) -- house rule.

Two things are pinned here, and the second is the one that would actually break
in production: the arithmetic, and the fact that the `tokens` filter is
reachable from EVERY Jinja environment in the app. Gootier builds six separate
Jinja2Templates instances, one per router, each with its own filter dict -- so a
filter registered on one of them is invisible to the other five, and the failure
mode is a TemplateAssertionError at render time on a page nobody re-tested.
"""
import pathlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from auth import get_current_user, get_current_user_optional
from database import Base, get_db
from display import format_tokens
import models  # noqa: F401
from models import User
from services import token_wallet


# --------------------------------------------------------------------------- #
# The arithmetic -- the table from the house rule, verbatim
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    (550, "550"),
    (2_000, "2K"),
    (750_000, "750K"),
    (20_000_000, "20M"),
])
def test_the_house_rule_table(value, expected):
    assert format_tokens(value) == expected


@pytest.mark.parametrize("value,expected", [
    (0, "0"),
    (999, "999"),
    (1_000, "1K"),
    (1_500, "1.5K"),
    (1_234_567, "1.2M"),
    (999_500, "999.5K"),
    # rounding must promote rather than produce "1000K"
    (999_999, "1M"),
    (1_000_000, "1M"),
])
def test_thresholds_and_rounding(value, expected):
    assert format_tokens(value) == expected


def test_unknown_is_an_em_dash_never_a_zero():
    """None means the Token Service could not answer. `0` would read as "you
    are out of tokens", which is a different and false statement."""
    assert format_tokens(None) == "—"
    assert format_tokens(0) == "0"


def test_a_trailing_point_zero_is_dropped():
    assert format_tokens(2_000) == "2K"
    assert not format_tokens(2_000).endswith(".0K")


# --------------------------------------------------------------------------- #
# Every Jinja environment in the app can actually resolve the filter
# --------------------------------------------------------------------------- #

def _route_modules_with_templates():
    routes_dir = pathlib.Path(__file__).resolve().parent.parent / "routes"
    found = []
    for path in sorted(routes_dir.glob("*.py")):
        src = path.read_text()
        # module-level instances only; the one inside oauth_routes._google_auth_fail
        # is function-local and renders login.html, which shows no token counts
        # matches both a bare constructor and one wrapped in install_filters(...)
        if re.search(r"^templates\s*=\s*\S*\(?Jinja2Templates\(", src, re.M):
            found.append("routes." + path.stem)
    return found


def test_the_scan_actually_finds_the_environments():
    """A guard on the guard: if this regex ever stops matching, the test below
    would pass by iterating over nothing."""
    assert len(_route_modules_with_templates()) >= 5


@pytest.mark.parametrize("module_name", _route_modules_with_templates())
def test_every_jinja_environment_has_the_tokens_filter(module_name):
    mod = __import__(module_name, fromlist=["templates"])
    assert "tokens" in mod.templates.env.filters, (
        f"{module_name} builds its own Jinja environment without the tokens "
        f"filter; any template it renders that uses |tokens will 500"
    )


# --------------------------------------------------------------------------- #
# The real pages, rendered through their own router's environment
# --------------------------------------------------------------------------- #

class FakeJTSClient:
    def __init__(self, raw):
        self.raw = raw

    def ensure_wallet(self, external_user_id, email="", customer_ref=None):
        return 320

    def get_balance(self, wallet_id):
        return self.raw


@pytest.fixture
def render(monkeypatch):
    from routes import media_routes, stripe_routes, web_routes

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)

    s = TestingSession()
    u = User(username="renderer", email="renderer@test.com", hashed_password="x",
             role="client", tier="trial", jts_wallet_id=320)
    s.add(u)
    s.commit()
    uid = u.id
    s.close()

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    def override_user():
        db = TestingSession()
        try:
            return db.query(User).filter(User.id == uid).first()
        finally:
            db.close()

    # /billing reads TOKEN_SERVICE_URL through env_config, which opens the real
    # SessionLocal -- this suite's DB has no env_configs table (the pre-existing
    # gap that also fails tests/test_affiliates_integration.py).
    from services import env_config
    monkeypatch.setattr(env_config, "get_env", lambda key, default="": default)

    app = FastAPI()
    app.include_router(web_routes.router)
    app.include_router(media_routes.router)
    app.include_router(stripe_routes.router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_optional] = override_user
    app.dependency_overrides[get_current_user] = override_user

    def _render(path, raw_balance):
        monkeypatch.setattr(token_wallet, "_client", lambda: FakeJTSClient(raw_balance))
        with TestClient(app) as c:
            resp = c.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
        return resp.text

    return _render


# Where each page prints the number, so a match elsewhere in the document
# cannot make one of these pass by accident.
_BALANCE_SLOT = {
    "/dashboard": r'<div class="stat-value">([^<]*)</div>\s*<div class="stat-label">Tokens available',
    "/studio":    r'<span class="hl hl-indigo">([^<]*) tokens</span>',
    "/assets":    r'<span class="hl hl-indigo">([^<]*) tokens</span>',
    "/billing":   r'<div class="display-5 fw-bold mb-3">([^<]*)<span',
}


def _slot(path, html):
    m = re.search(_BALANCE_SLOT[path], html)
    assert m, f"could not find the balance slot in {path}"
    return m.group(1).strip()


@pytest.mark.parametrize("path", list(_BALANCE_SLOT))
@pytest.mark.parametrize("raw,expected", [
    # raw tokens -> display units (raw // 1000) -> rendered
    (250_000, "250"),          #        250 display
    (1_500_000, "1.5K"),       #      1,500 display
    (750_000_000, "750K"),     #    750,000 display
    (20_000_000_000, "20M"),   # 20,000,000 display
])
def test_balances_render_compactly(render, path, raw, expected):
    assert _slot(path, render(path, raw)) == expected


@pytest.mark.parametrize("path", list(_BALANCE_SLOT))
def test_a_thousands_separated_form_is_never_rendered(render, path):
    """The pattern this rule replaces. 1,500 display units must not appear as
    "1,500" or "1500" anywhere in the balance slot."""
    slot = _slot(path, render(path, 1_500_000))
    assert "," not in slot
    assert slot == "1.5K"
