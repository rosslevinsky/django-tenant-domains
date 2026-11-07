"""Test matrix. Pairs come from Django's supported-versions table, without end-of-life Pythons."""

import nox

nox.options.default_venv_backend = "uv"
nox.options.sessions = ["tests", "lint"]

PAIRS = [
    ("5.2", "3.11"),
    ("5.2", "3.12"),
    ("5.2", "3.13"),
    ("5.2", "3.14"),
    ("6.0", "3.12"),
    ("6.0", "3.13"),
    ("6.0", "3.14"),
    ("6.1", "3.12"),
    ("6.1", "3.13"),
    ("6.1", "3.14"),
    ("6.1", "3.15"),
]


@nox.session
@nox.parametrize(
    "python,django",
    [(python, django) for django, python in PAIRS],
    ids=[f"{django}-py{python}" for django, python in PAIRS],
)
def tests(session, django):
    session.install("-e", ".[dev]", f"Django~={django}.0")
    session.run("pytest", *session.posargs)


@nox.session(python="3.12")
def lint(session):
    session.install("ruff")
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")
