#!/usr/bin/env python3
"""Publish generated output files on top of the latest remote main safely.

Collectors can finish concurrently with other output publishers.  The old
commit-then-rebase flow conflicted when main advanced.  This helper preserves
only the caller's generated files, refreshes from origin/main, reapplies those
files, and retries a rejected push without rewriting unrelated output.
"""

import argparse
import pathlib
import shutil
import subprocess
import tempfile
import time


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def validate_paths(paths, root):
    resolved = []
    for raw in paths:
        relative = pathlib.PurePosixPath(raw.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"output path must stay inside the repository: {raw}")
        source = (root / pathlib.Path(*relative.parts)).resolve()
        if root not in source.parents or not source.is_file():
            raise ValueError(f"output file does not exist inside the repository: {raw}")
        resolved.append((relative, source))
    return resolved


def publish(paths, message, attempts=4, branch="main", remote="origin"):
    root = pathlib.Path(run("git", "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    validated = validate_paths(paths, root)
    with tempfile.TemporaryDirectory(prefix="btc-lean-amt-publish-") as directory:
        saved_root = pathlib.Path(directory)
        for relative, source in validated:
            saved = saved_root / pathlib.Path(*relative.parts)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, saved)

        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        for attempt in range(1, attempts + 1):
            run("git", "fetch", remote, branch)
            run("git", "checkout", "-B", branch, f"{remote}/{branch}")
            for relative, _ in validated:
                destination = root / pathlib.Path(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved_root / pathlib.Path(*relative.parts), destination)
            run("git", "add", "--", *[str(relative) for relative, _ in validated])
            if run("git", "diff", "--cached", "--quiet", check=False).returncode == 0:
                print("No scoped output changes to publish")
                return None
            run("git", "commit", "-m", message)
            pushed = run("git", "push", remote, f"HEAD:{branch}", check=False)
            if pushed.returncode == 0:
                sha = run("git", "rev-parse", "HEAD").stdout.strip()
                print(f"Published {sha}")
                return sha
            if attempt == attempts:
                raise RuntimeError(f"push failed after {attempts} attempts: {pushed.stderr.strip()}")
            time.sleep(attempt)
    raise RuntimeError("unreachable")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", required=True)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    publish(args.paths, args.message, attempts=args.attempts)


if __name__ == "__main__":
    main()
