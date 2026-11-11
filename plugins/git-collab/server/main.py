"""Launch the installed shared core; the plugin contains no second Git engine."""

import sys

try:
    from ddtui.collab_mcp import main
except ImportError:
    print("Install ddtui with a supported Python and use the ddtui-collab-mcp console entry point.", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
