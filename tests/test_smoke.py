import os
import subprocess
import sys
from pathlib import Path

from django.apps import apps

EXAMPLE = Path(__file__).resolve().parent.parent / "example"


def test_app_loads():
    assert apps.get_app_config("django_tenant_domains").name == "django_tenant_domains"


def test_example_project_passes_check():
    # pytest-django exports the test settings, which would hide the example project's own.
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    result = subprocess.run(
        [sys.executable, "manage.py", "check"],
        check=False,
        cwd=EXAMPLE,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
