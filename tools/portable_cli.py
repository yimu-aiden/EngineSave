"""Entry point of enginesave-cli.exe, the command line of the portable build (enginesave.spec)."""

import sys

from enginesave.cli import main

if __name__ == "__main__":
    sys.exit(main())
