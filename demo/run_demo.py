#!/usr/bin/env python
"""Run the offline dependency-diff explainer on the demo environments.

Usage:
    python demo/run_demo.py [--json]

The demo compares two target environments:

* ``env_a.json`` -- Python 3.10 with the ``fast`` extra requested
* ``env_b.json`` -- Python 3.12 with no extras requested

and reports which requirements become enabled / disabled / unknown because
of their markers.  Nothing is downloaded or resolved.
"""

from __future__ import annotations

import os
import sys

# Make the local ``src/packaging`` importable without installing it.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from packaging.diffexplain import main  # noqa: E402

if __name__ == "__main__":
    demo_dir = os.path.dirname(__file__)
    env_a = os.path.join(demo_dir, "env_a.json")
    env_b = os.path.join(demo_dir, "env_b.json")
    requirements = os.path.join(demo_dir, "requirements.txt")
    sys.exit(main([env_a, env_b, requirements, *sys.argv[1:]]))
