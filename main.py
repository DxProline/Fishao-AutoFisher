from __future__ import annotations

import sys

from application_start import ApplicationStart


def main() -> int:
    return ApplicationStart.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
