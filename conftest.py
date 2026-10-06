"""
Repository-root pytest conftest.

The sandbox's default Python environment (``/opt/venv``, first on PATH) is
read-only and does not have ``aws-cdk-lib``/``constructs``/``moto``
installed (``pip install`` there fails with ``OSError: Read-only file
system``). This project therefore keeps its dependencies in a
project-local virtualenv at ``.venv/`` (per the standard "create a
project-local venv" pattern for a read-only default interpreter).

If the test runner invokes the default ``pytest`` (resolved from
``/opt/venv/bin``) instead of the project's ``.venv``, the CDK/moto
imports in the test modules would fail with ``ModuleNotFoundError``. To
make the test suite runnable either way, this conftest adds ``.venv``'s
site-packages directory to ``sys.path`` (if present and not already
importable) before any test module is collected.
"""

import glob
import os
import site
import sys


def _ensure_local_venv_on_path() -> None:
    try:
        import aws_cdk  # noqa: F401
        import moto  # noqa: F401

        return  # already importable, nothing to do
    except ImportError:
        pass

    repo_root = os.path.dirname(os.path.abspath(__file__))
    candidates = glob.glob(
        os.path.join(repo_root, ".venv", "lib", "python*", "site-packages")
    )
    for site_packages in candidates:
        if os.path.isdir(site_packages) and site_packages not in sys.path:
            site.addsitedir(site_packages)
            sys.path.insert(0, site_packages)


_ensure_local_venv_on_path()
