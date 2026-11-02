#!/usr/bin/env python3
"""Bounded no-follow top-result triage before full handoff validation.

Prints pass, fail or error for a well-formed version-1 result; contract-fail or
contract when the verdict is readable but the contract version is wrong (a fail
stays blocking either way); oversized; or invalid. With --reason, prints the
verdict reason of a readable result instead, or nothing.
"""
import sys

import contract


def read(path):
    path = contract.absolute(path)
    if path.is_symlink():
        return "invalid", None
    try:
        if path.stat().st_size > contract.MIB:
            return "oversized", None
        result, _ = contract.read_json(path, contract.MIB)
        verdict = result.get("verdict")
        contract.require(isinstance(verdict, dict) and
                         verdict.get("status") in ("pass", "fail", "error"),
                         "unknown top verdict")
    except (ValueError, OSError, KeyError, TypeError, RecursionError):
        return "invalid", None
    try:
        contract.same_version(result, "contract_version")
    except ValueError:
        return ("contract-fail" if verdict["status"] == "fail" else "contract"), verdict
    return verdict["status"], verdict


def main():
    args = sys.argv[1:]
    want_reason = args[:1] == ["--reason"]
    status, verdict = read(args[-1])
    if not want_reason:
        print(status)
    elif verdict is not None and isinstance(verdict.get("reason"), str):
        print(verdict["reason"])


if __name__ == "__main__":
    main()
