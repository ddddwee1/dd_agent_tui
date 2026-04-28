"""Backward-compat shim — the implementation moved to the `ddtui` package.

`python agent.py` and `from agent import main` still work; the
`ddtui` console script and `python -m ddtui` are the preferred
entry points.
"""

from ddtui.app import main


if __name__ == "__main__":
    main()
