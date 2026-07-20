"""Enable `python -m agent_saga.ui [--wal-path ...] [--port ...]`.

Delegates to the shared CLI so there is one implementation of the `ui` command.
"""

import sys

from ..cli import main

if __name__ == "__main__":
    raise SystemExit(main(["ui", *sys.argv[1:]]))
