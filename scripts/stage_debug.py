#!/usr/bin/env python3
"""Copy regular review debug files into a host-private artifact directory.

The engine controls OUTPUT_DIR/run. Never hand that tree to an uploader that
follows links: walk source directories through no-follow descriptors and copy
only single-link regular files. The destination is outside the engine mount.
"""
import os
from pathlib import Path
import shutil
import stat
import tempfile


def open_directory(path):
    """Open every absolute path component without following directory links."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or any(c in str(path) for c in "\r\n\x00"):
        raise ValueError("unsafe source path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for name in path.parts[1:]:
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def copy_file(source, target, size):
    """Never read beyond the size observed before copying, even if a file grows."""
    with target.open("xb") as stream:
        remaining = size
        while remaining:
            data = os.read(source, min(remaining, 1024 * 1024))
            if not data:
                raise ValueError("source changed while copying")
            stream.write(data)
            remaining -= len(data)


def copy_tree(source, destination):
    """Iterative descriptor walk; source names and errors are never logged."""
    copied, skipped = 0, False
    # Each iterator and descriptor is owned by this stack until its frame ends.
    stack = []
    try:
        stack.append((source, destination, os.scandir(source)))
        source = None
        while stack:
            directory, target, entries = stack[-1]
            entry = next(entries, None)
            if entry is None:
                entries.close()
                os.close(directory)
                stack.pop()
                continue
            before = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                try:
                    folder = target / entry.name
                    folder.mkdir(mode=0o700)
                    iterator = os.scandir(child)
                except BaseException:
                    os.close(child)
                    raise
                stack.append((child, folder, iterator))
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                skipped = True
                continue
            fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                         getattr(os, "O_NOCTTY", 0),
                         dir_fd=directory)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    skipped = True
                    continue
                copy_file(fd, target / entry.name, before.st_size)
                after = os.fstat(fd)
                if ((before.st_dev, before.st_ino, before.st_mode, before.st_nlink,
                     before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_dev, after.st_ino, after.st_mode, after.st_nlink,
                     after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise ValueError("source changed while copying")
                copied += 1
            finally:
                os.close(fd)
        return copied, skipped
    finally:
        if source is not None:
            os.close(source)
        for directory, _target, entries in reversed(stack):
            entries.close()
            os.close(directory)


def main():
    os.umask(0o077)
    stage, source = None, None
    try:
        output = Path(os.environ["OUTPUT_DIR"])
        try:
            source = open_directory(output / "run")
        except FileNotFoundError:
            return 0  # Failed engines need not have created a debug directory.
        runner = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        if (any(c in str(runner) for c in "\r\n\x00") or
                runner == output or output in runner.parents):
            raise ValueError("staging root is not host private")
        stage = Path(tempfile.mkdtemp(prefix="korza-debug-", dir=runner))
        # copy_tree takes ownership of the descriptor, including failure paths.
        owned_source, source = source, None
        copied, skipped = copy_tree(owned_source, stage)
        if skipped:
            print("::warning::Some unsafe debug entries were omitted.")
        if not copied:
            return 0
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write("debug-dir=" + str(stage) + "\n")
        stage = None  # Keep the completed private snapshot for the upload step.
    except (OSError, ValueError, KeyError, RuntimeError):
        print("::warning::Debug files could not be staged safely; artifact upload skipped.")
    finally:
        if source is not None:
            os.close(source)
        if stage is not None:
            try:
                shutil.rmtree(stage)
            except Exception:
                print("::warning::Incomplete debug staging files could not be removed; artifact upload skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
