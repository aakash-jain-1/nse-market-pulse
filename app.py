#!/usr/bin/env python
"""Repo-root entry point for the NSE Market Pulse dashboard.

Thin shim only: the Flask app and all its startup logic live in
``nse_pulse/web/app.py``. This keeps ``python app.py`` working after the code
moved into the package, and the Werkzeug reloader re-executes this same file on
edits (it watches every imported ``nse_pulse`` module).
"""

from nse_pulse.web.app import main

if __name__ == "__main__":
    main()
