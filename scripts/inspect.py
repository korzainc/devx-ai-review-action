#!/usr/bin/env python3
"""Bounded no-follow top-result triage before full handoff validation."""
from pathlib import Path
import sys

import contract


def main():
    path = contract.absolute(sys.argv[1])
    reason_only = sys.argv[2:] == ["--reason"]
    if path.is_symlink():
        print("" if reason_only else "invalid")
        return
    try:
        if path.stat().st_size > contract.MIB:
            print("" if reason_only else "oversized")
            return
        result, _ = contract.read_json(path, contract.MIB)
        verdict = result.get("verdict")
        contract.require(isinstance(verdict, dict) and
                         verdict.get("status") in ("pass", "fail", "error"),
                         "unknown top verdict")
        # Triage preserves a known blocking status even when completion later
        # rejects the contract version. Reasons go only to the private summary.
        print(verdict.get("reason", "") if reason_only and
              isinstance(verdict.get("reason", ""), str) else
              "" if reason_only else verdict["status"])
    except (ValueError, OSError, KeyError, TypeError, RecursionError):
        print("" if reason_only else "invalid")


if __name__ == "__main__":
    main()
