#!/usr/bin/env python3
"""Retired migration entry point, kept safe for older startup wrappers.

Remote message IDs must never change after Android has synchronized them.
server_id and per-profile local message IDs provide the namespace instead.
IDs previously shifted by 0.4.x/0.5.0 remain valid and are not normalized.
"""


def main() -> int:
    print("[yuGoHA ids] retired migration: existing message IDs preserved", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
