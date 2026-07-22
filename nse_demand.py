#!/usr/bin/env python
"""Repo-root entry point for the NSE demand CLI scanner.

Thin shim over ``nse_pulse/cli/nse_demand.py`` so ``python nse_demand.py [view]``
keeps working after the code moved into the package.
"""

from nse_pulse.cli.nse_demand import main

if __name__ == "__main__":
    main()
