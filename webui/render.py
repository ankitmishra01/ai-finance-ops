"""One Jinja environment for the web UI and the static site, so both share templates and styles."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from webui import evidence

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"


def pct(v) -> str:
    return f"{float(v) * 100:.1f}%"


def env() -> Environment:
    e = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]),
                    trim_blocks=True, lstrip_blocks=True)
    e.filters.update(cents=evidence.cents, dollars=evidence.dollars, pct=pct, humanise=evidence.humanise)
    e.globals.update(MODULES=evidence.MODULES, KIND_LABELS=evidence.KIND_LABELS)
    return e


ENV = env()


def render(name: str, **ctx) -> str:
    return ENV.get_template(name).render(**ctx)
