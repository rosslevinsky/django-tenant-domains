"""The example project's own tests, run in its own process with its own settings."""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "example"


def _manage(*args):
    # pytest-django exports the test settings, which would hide the example project's own.
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        check=False,
        cwd=EXAMPLE,
        env=env,
        capture_output=True,
        text=True,
    )


def test_address_book_isolates_two_tenants():
    result = _manage("test", "address_book")
    assert result.returncode == 0, result.stderr


def test_migrations_are_complete():
    result = _manage("makemigrations", "--check", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_personal_paths():
    """The example runs anywhere, so nothing in it may name someone's home folder."""
    home = re.compile(r"/(?:Users|home)/\w")
    for path in EXAMPLE.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".html", ".md", ".txt"}:
            assert not home.search(path.read_text()), path
