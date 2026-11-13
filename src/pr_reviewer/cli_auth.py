"""Generate a DEMO_ACCOUNTS password hash: ``python -m pr_reviewer.cli_auth``.

The password is read with getpass (never echoed, never an argument) and only the hash is
printed. Put ``username:<hash>`` in the DEMO_ACCOUNTS environment variable.
"""

from __future__ import annotations

import getpass
import sys

from .auth import hash_password

MIN_LENGTH = 12


def main() -> int:
    password = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if len(password) < MIN_LENGTH:
        print(f"Use at least {MIN_LENGTH} characters.", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
