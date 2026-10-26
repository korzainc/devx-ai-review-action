#!/usr/bin/env python3
"""Bounded no-follow top-result triage before full handoff validation."""
from pathlib import Path
import sys

import contract


def main():
    path = contract.absolute(sys.argv[1])
    if path.is_symlink():
        print("invalid")
        return
    try:
        if path.stat().st_size > contract.MIB:
            print("oversized")
            return
        result, _ = contract.read_json(path, contract.MIB)
        contract.same_version(result, "contract_version")
        verdict = result.get("verdict")
        contract.require(isinstance(verdict, dict) and
                         verdict.get("status") in ("pass", "fail", "error"),
                         "unknown top verdict")
        print(verdict["status"])
    except (ValueError, OSError, KeyError, TypeError, RecursionError):
        print("invalid")


if __name__ == "__main__":
    main()
