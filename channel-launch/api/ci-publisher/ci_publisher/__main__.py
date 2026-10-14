"""Entry point for `python3 -m ci_publisher`."""

import sys

from ci_publisher.cli import main

if __name__ == "__main__":
    sys.exit(main())
