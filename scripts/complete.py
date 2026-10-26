#!/usr/bin/env python3
"""Attest a completed analysis outside the engine mount after contract validation.

The caller invokes this only after action-review exits zero and renders its
summary. The final marker appears only after contract.py validates the staged
handoff against frozen host identity. This file has no GitHub/model authority.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys

import contract


def main():
    parser = argparse.ArgumentParser()
    for name in ("output-dir", "control-dir", "repository", "pr-number", "head-sha",
                 "merge-base-sha", "image", "github-run-id", "github-run-attempt"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    output = contract.absolute(args.output_dir)
    control = contract.absolute(args.control_dir)
    contract.require(not control.is_relative_to(output), "control dir is engine writable")
    identity = contract.identity(args)
    top, top_bytes = contract.read_json(output / "result.json", contract.MIB)
    typed, typed_bytes = contract.read_json(output / "publish/result.json", 16 * contract.MIB)
    _, snapshot_bytes = contract.read_json(output / "publish/snapshot.json", 16 * contract.MIB)
    contract.action_version(top)
    verdict = top.get("verdict")
    contract.require(isinstance(verdict, dict) and verdict.get("status") in ("pass", "fail"),
                     "review was not complete")
    contract.require(contract.identifier(typed.get("run_id")), "missing engine run ID")
    manifest = {"contract_version": 1, "engine_exit": 0,
                "review_verdict": verdict["status"], "engine_run_id": typed["run_id"],
                "action_result_sha256": contract.digest(top_bytes),
                "result_sha256": contract.digest(typed_bytes),
                "snapshot_sha256": contract.digest(snapshot_bytes), **identity}
    pending = control / "pending.json"
    final = control / "completion.json"
    contract.require(not final.exists() and not pending.exists(), "completion marker already exists")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(manifest, stream, sort_keys=True)
        stream.write("\n")
    preflight = control / "preflight"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            contract.stage_handoff(argparse.Namespace(**vars(args), completion_file=str(pending),
                                                       stage_dir=str(preflight)))
        shutil.rmtree(preflight)
        top_copy = control / "result.json"
        fd = os.open(top_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(top_bytes)
        _, copied = contract.read_json(top_copy, contract.MIB)
        contract.require(contract.digest(copied) == manifest["action_result_sha256"],
                         "host result copy changed")
        os.replace(pending, final)
    finally:
        if pending.exists():
            pending.unlink()
        if preflight.exists():
            shutil.rmtree(preflight)
    print(final)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as exc:
        print("Review completion rejected: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
