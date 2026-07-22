"""Repo-root-anchored filesystem paths.

Centralised so that data/, config, state and log files resolve to the project
root regardless of where the importing module lives inside the ``nse_pulse``
package. Modules that used to derive these from ``os.path.dirname(__file__)``
now go through here, which keeps those files at the repo root after the code
moved into the package tree.
"""

import os

# This file is nse_pulse/core/paths.py, so the repo root is three levels up:
#   paths.py -> core -> nse_pulse -> <repo root>
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def root(*parts):
    """Absolute path to ``*parts`` joined under the project root."""
    return os.path.join(PROJECT_ROOT, *parts)
