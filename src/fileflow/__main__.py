"""Allow ``python -m fileflow``."""

import sys

from fileflow.cli import main

if __name__ == "__main__":
    sys.exit(main())
