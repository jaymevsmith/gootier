"""Shared presentation helpers.

Presentation only. Nothing here ever changes a stored value or an API payload:
the Jhome Token Service's `balance_display` stays an integer on the wire so
existing consumers keep working, and this module formats it on the way to a
template.
"""
from fastapi.templating import Jinja2Templates


def format_tokens(n) -> str:
    """Compact token display (house rule): 550 -> "550", 2000 -> "2K", 20000000 -> "20M".

    Presentation only -- never changes a stored value or an API payload.
    """
    if n is None:
        return "—"
    n = float(n)
    a = abs(n)
    if a < 1_000:
        return str(int(round(n)))
    if a >= 1_000_000:
        v, suffix = n / 1_000_000, "M"
    else:
        v, suffix = n / 1_000, "K"
        if abs(round(v, 1)) >= 1000:      # 999,999 must render "1M", not "1000K"
            v, suffix = n / 1_000_000, "M"
    s = f"{v:.1f}"
    return (s[:-2] if s.endswith(".0") else s) + suffix


def install_filters(templates: Jinja2Templates) -> Jinja2Templates:
    """Register the house filters on one Jinja environment, and return it.

    Every router in this app builds its OWN Jinja2Templates, and each one gets a
    private copy of the filter dict -- registering on one is invisible to the
    others, and the failure mode is a TemplateAssertionError at render time on
    whichever page was not re-tested. So this is called at each instantiation
    site rather than once centrally, and tests/test_display.py walks the routes
    package to assert none was missed.

    Returns the instance so it can wrap the constructor in one expression.
    """
    templates.env.filters["tokens"] = format_tokens
    return templates
