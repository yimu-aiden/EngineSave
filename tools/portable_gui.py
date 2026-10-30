"""Entry point of EngineSave.exe, the window of the portable build (tools/enginesave.spec)."""

import sys

from enginesave.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
