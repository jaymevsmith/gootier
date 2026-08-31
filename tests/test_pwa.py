"""Gootier is installable from every page a visitor can reach.

A PWA fails silently: a page that does not link the manifest simply never offers
to install, with no error anywhere. Gootier had the manifest, the icons and a
root-scoped worker already -- but the AUTH pages extend a third base template
that carried none of it, and `/` redirects to /login. So the only pages an
unauthenticated visitor ever saw were the ones that could not install.
"""
import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"


def _read(name):
    return (TEMPLATES / name).read_text()


def test_every_base_template_links_the_manifest():
    """There are THREE bases here: base.html, public_base.html and
    auth_base.html. Missing any one leaves a whole class of page uninstallable."""
    for base in ("base.html", "public_base.html", "auth_base.html"):
        html = _read(base)
        assert 'rel="manifest"' in html, f"{base} does not link the manifest"
        assert "/static/manifest.webmanifest" in html, f"{base} manifest href is wrong"


def test_every_base_template_registers_the_worker():
    for base in ("base.html", "public_base.html", "auth_base.html"):
        html = _read(base)
        assert "serviceWorker" in html, f"{base} never registers the worker"
        # Root path, not /static/sw.js: a worker only controls URLs at or below
        # its own path, so one under /static/ would control nothing useful.
        assert "'/sw.js'" in html or '"/sw.js"' in html, f"{base} registers the wrong path"


def test_the_sign_in_page_inherits_it():
    """login.html extends auth_base.html, which is the one that was missing it."""
    login = _read("login.html")
    assert 'extends "auth_base.html"' in login, (
        "login.html no longer extends auth_base.html -- re-check which base carries the manifest")


def test_the_manifest_is_installable():
    import json
    m = json.loads((TEMPLATES.parent / "static" / "manifest.webmanifest").read_text())
    assert m["display"] == "standalone"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    # Without a maskable icon Android letterboxes the app icon.
    assert any(i.get("purpose") == "maskable" for i in m["icons"]), "needs a maskable icon"


def test_every_declared_icon_exists():
    """A 404 icon silently invalidates the whole manifest, with no error."""
    import json
    static = TEMPLATES.parent / "static"
    m = json.loads((static / "manifest.webmanifest").read_text())
    for icon in m["icons"]:
        rel = icon["src"].removeprefix("/static/")
        assert (static / rel).exists(), f"manifest declares a missing icon: {icon['src']}"
