#!/usr/bin/env python3
"""Host checks for engine 4ebd526's version-1 action/publisher seam.

The future review controller MUST write REVIEW_COMPLETION_FILE outside every
engine-writable mount, only after waiting for this invocation's process. This
module never infers completion from files. The host manifest has integer
contract_version=1, engine_exit=0; review_verdict=pass|fail; repository,
pr_number (integer), head_sha, merge_base_sha, image (immutable full reference),
github_run_id, github_run_attempt, engine_run_id; and SHA-256 of exact bytes in
result_sha256 (publish/result.json), snapshot_sha256 (publish/snapshot.json),
action_result_sha256 (result.json). Identity comes from frozen host preparation,
not the files under review. engine_run_id and hashes are captured after exit.

stage-handoff checks that attestation and copies just the two publisher inputs
to a fresh host-private directory. It does not create a completion attestation.
Limits are controller policy: top result 1 MiB, each handoff 16 MiB, host manifest
64 KiB, publisher receipt 256 KiB. Oversize requires investigation, no truncation.
The engine remains responsible for its complete typed schema and text cleaning;
we validate the identity, completed verdict and finding fields at this boundary.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

MIB = 1024 * 1024
REVIEWERS = {"spec-adherence", "code-reviewer", "standards-and-practices",
             "test-coverage", "performance-review", "bug-hunter",
             "owasp-reviewer", "stride-modeler"}
SEVERITIES = {"critical", "high", "medium", "low", "info"}


class ContractError(ValueError):
    """A fixed developer-written reason, never an untrusted value or path."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def text(value):
    return isinstance(value, str)


def identifier(value):
    return text(value) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def absolute(value):
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts, "paths must be absolute without traversal")
    require(not any(c in str(path) for c in (":", "\r", "\n", "\x00")), "unsafe mount path")
    return path


def read_json(path, limit):
    """Walk with no-follow descriptors; never block on a FIFO or follow a symlink."""
    path = absolute(path)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                    "input must be a regular unlinked-alias-free file")
            require(0 < before.st_size <= limit, "input exceeds size limit or is empty")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            require(len(raw) <= limit and (before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "input changed while reading")
    finally:
        os.close(fd)

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("non-finite JSON number")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    require(isinstance(value, dict), "expected JSON object")
    return value, raw


def same_version(obj, field):
    require(type(obj.get(field)) is int and obj[field] == 1, "unsupported contract version")


def findings(items, typed):
    require(isinstance(items, list), "findings must be a list")
    ids = []
    for item in items:
        require(isinstance(item, dict) and identifier(item.get("id")), "invalid finding identifier")
        ids.append(item["id"])
        location = item if typed else item.get("location")
        require(isinstance(location, dict), "invalid finding location")
        path = location.get("file" if typed else "path")
        require(text(path) and path and not Path(path).is_absolute() and
                ".." not in Path(path).parts and "\x00" not in path, "invalid finding path")
        start, end = location.get("start_line"), location.get("end_line")
        require(integer(start, 1) and integer(end, start), "invalid finding lines")
        require(item.get("severity") in SEVERITIES and
                item.get("reviewer" if typed else "skill") in REVIEWERS, "invalid finding classification")
        confidence = item.get("confidence")
        require(type(confidence) in (int, float) and 0 <= confidence <= 1, "invalid confidence")
        for field in (("unit_id", "category", "title", "body") if typed else ("rule_id", "message")):
            require(text(item.get(field)), "missing finding text")
        require(item.get("suggestion") is None or text(item["suggestion"]), "invalid suggestion")
        if not typed:
            require(type(item.get("enforcing")) is bool, "invalid enforcing flag")
    require(len(set(ids)) == len(ids), "duplicate finding identifiers")
    return ids


def identity(args):
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) is not None,
            "invalid repository")
    require(re.fullmatch(r"[1-9][0-9]*", args.pr_number) is not None, "invalid PR number")
    for value in (args.head_sha, args.merge_base_sha):
        require(re.fullmatch(r"[0-9a-f]{40}", value) is not None, "invalid commit SHA")
    for value in (args.github_run_id, args.github_run_attempt):
        require(re.fullmatch(r"[1-9][0-9]*", value) is not None, "invalid GitHub invocation")
    require(re.fullmatch(r"[a-z0-9][a-z0-9./_:-]*@sha256:[0-9a-f]{64}", args.image) is not None,
            "image must be pinned by SHA-256")
    return {"repository": args.repository, "pr_number": int(args.pr_number),
            "head_sha": args.head_sha, "merge_base_sha": args.merge_base_sha,
            "github_run_id": args.github_run_id, "github_run_attempt": args.github_run_attempt,
            "image": args.image}


def stage_handoff(args):
    # The current publisher uses the engine's GitHub.com API default. Reject
    # alternate trust domains before making any write authority available.
    require(getattr(args, "server_url", "https://github.com") == "https://github.com",
            "publisher supports only https://github.com")
    require(getattr(args, "api_url", "https://api.github.com") == "https://api.github.com",
            "publisher supports only https://api.github.com")
    expected = identity(args)
    output, completion, stage = map(absolute, (args.output_dir, args.completion_file, args.stage_dir))
    require(not completion.is_relative_to(output) and not stage.is_relative_to(output),
            "host control files must be outside engine output")
    manifest, _ = read_json(completion, 64 * 1024)
    same_version(manifest, "contract_version")
    require(all(manifest.get(k) == v and type(manifest.get(k)) is type(v) for k, v in expected.items()),
            "completion identity mismatch")
    require(type(manifest.get("engine_exit")) is int and manifest["engine_exit"] == 0,
            "host did not attest process success")
    require(manifest.get("review_verdict") in ("pass", "fail") and
            identifier(manifest.get("engine_run_id")), "host did not attest a completed review")
    action, top_raw = read_json(output / "result.json", MIB)
    result, result_raw = read_json(output / "publish/result.json", 16 * MIB)
    snapshot, snapshot_raw = read_json(output / "publish/snapshot.json", 16 * MIB)
    for key, raw in (("action_result", top_raw), ("result", result_raw), ("snapshot", snapshot_raw)):
        require(manifest.get(key + "_sha256") == digest(raw), "review bytes changed after completion")
    same_version(action, "contract_version")
    same_version(result, "schema_version")
    same_version(snapshot, "schema_version")
    verdict = action.get("verdict")
    require(isinstance(verdict, dict) and verdict.get("status") == manifest["review_verdict"] and
            text(verdict.get("reason")), "review is incomplete or verdict differs")
    require(result.get("head_sha") == args.head_sha and
            result.get("run_id") == manifest["engine_run_id"], "review identity mismatch")
    pr, refs = snapshot.get("pr"), snapshot.get("refs")
    require(snapshot.get("provider") == "github" and snapshot.get("repo") == args.repository and
            isinstance(pr, dict) and type(pr.get("number")) is int and
            pr["number"] == int(args.pr_number), "snapshot PR mismatch")
    require(isinstance(refs, dict) and refs.get("head_sha") == args.head_sha and
            refs.get("merge_base_sha") == args.merge_base_sha, "snapshot commit mismatch")
    for field in ("base_ref", "head_ref", "base_sha"):
        require(text(refs.get(field)) and refs[field], "missing snapshot refs")
    for field in ("url", "title"):
        require(text(pr.get(field)), "missing snapshot PR fields")
    for field in ("diff_path", "workspace"):
        require(text(snapshot.get(field)), "missing snapshot path")
    require(isinstance(result.get("provenance"), dict) and
            all(text(result["provenance"].get(k)) for k in ("package_version", "provider")),
            "missing review provenance")
    for field in ("dropped", "skipped", "not_reviewed"):
        require(isinstance(result.get(field, []), list), "invalid review list")
    require(type(result.get("stopped_at_budget", False)) is bool, "invalid review budget flag")
    require(isinstance(action.get("skills_run"), list) and
            all(s in REVIEWERS for s in action["skills_run"]) and
            isinstance(action.get("provenance"), dict), "invalid action metadata")
    typed_ids = findings(result.get("findings"), typed=True)
    top_ids = findings(action.get("findings"), typed=False)
    require(set(typed_ids) == set(top_ids), "top and typed findings differ")
    blocking = verdict.get("blocking_findings")
    require(isinstance(blocking, list) and all(text(v) and v in typed_ids for v in blocking),
            "invalid blocking findings")
    # Validate before any copy, then recheck the staged bytes immediately before returning.
    stage.mkdir(mode=0o700, exist_ok=True)
    require(stage.is_dir() and not stage.is_symlink() and not any(stage.iterdir()),
            "staging directory must be fresh")
    (stage / "publish").mkdir(mode=0o700)
    (stage / "run").mkdir(mode=0o700)
    for name, raw in (("result.json", result_raw), ("snapshot.json", snapshot_raw)):
        target = stage / "publish" / name
        with target.open("xb") as stream:
            stream.write(raw)
        target.chmod(0o400)
        _, copied = read_json(target, 16 * MIB)
        require(digest(copied) == digest(raw), "staged bytes changed")
    print(len(typed_ids))


def validate_receipt(args):
    require(args.publisher_exit == "0", "publisher exited unsuccessfully; delivery may be partial")
    receipt, _ = read_json(Path(args.stage_dir) / "run/publish/publish.json", 256 * 1024)
    require(receipt.get("publisher") == "github", "unexpected publisher")
    posted, couldnt = receipt.get("posted_comments"), receipt.get("couldnt_place")
    already = receipt.get("already_posted", 0)
    require(integer(posted) and integer(couldnt) and integer(already), "invalid publication counts")
    moved, summary_only = receipt.get("head_moved"), receipt.get("summary_only")
    require(type(moved) is bool and type(summary_only) is bool, "invalid staleness flags")
    require(isinstance(receipt.get("notes"), list) and all(text(v) for v in receipt["notes"]),
            "invalid receipt notes")
    if summary_only:
        require(moved and posted == 0 and couldnt == 0 and already == 0,
                "inconsistent stale summary receipt")
    else:
        require(posted + couldnt + already == int(args.findings_count),
                "receipt does not account for findings")
    require(args.server_url == "https://github.com", "publisher supports only https://github.com")
    expected = args.server_url + "/" + args.repository + "/pull/" + args.pr_number
    require(text(receipt.get("url")) and
            re.fullmatch(re.escape(expected) + r"#issuecomment-[1-9][0-9]*", receipt["url"]) is not None,
            "receipt has no verified summary-comment destination")
    # Retain only these validated fields after the temporary engine export is removed.
    saved = {"contract_version": 1, "repository": args.repository,
             "pr_number": int(args.pr_number), "publisher": "github", "posted_comments": posted,
             "couldnt_place": couldnt, "already_posted": already,
             "head_moved": moved, "summary_only": summary_only,
             "url": receipt["url"]}
    receipt_file = absolute(args.stage_dir) / "receipt.json"
    fd = os.open(receipt_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(saved, stream, sort_keys=True)
        stream.write("\n")
    # Only safe scalar outputs, never arbitrary engine strings or notes.
    print("delivery=success")
    print("summary-url=" + receipt["url"])
    print("posted-comments=" + str(posted))
    print("couldnt-place=" + str(couldnt))
    print("head-moved=" + str(moved).lower())
    print("summary-only=" + str(summary_only).lower())
    print("receipt-file=" + str(receipt_file))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage-handoff")
    for name in ("output-dir", "completion-file", "stage-dir", "repository", "pr-number",
                 "head-sha", "merge-base-sha", "image", "github-run-id", "github-run-attempt"):
        stage.add_argument("--" + name, required=True)
    stage.add_argument("--server-url", default="https://github.com")
    stage.add_argument("--api-url", default="https://api.github.com")
    receipt = commands.add_parser("validate-receipt")
    for name in ("stage-dir", "repository", "pr-number", "server-url", "findings-count", "publisher-exit"):
        receipt.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        (stage_handoff if args.command == "stage-handoff" else validate_receipt)(args)
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as exc:
        # Only require()'s fixed messages are safe; dynamic exceptions can contain
        # paths, untrusted data, engine messages or credentials.
        reason = str(exc) if isinstance(exc, ContractError) else type(exc).__name__
        print("Publisher contract rejected: " + reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
