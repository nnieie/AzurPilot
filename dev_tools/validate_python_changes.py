#!/usr/bin/env python3
"""Validate changed Python files for syntax and undefined names."""

from __future__ import annotations

import argparse
import py_compile
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="HEAD^1", help="Base git revision for diff comparison.")
    parser.add_argument("--head", default="HEAD", help="Head git revision for diff comparison.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional explicit file paths to validate. If omitted, files are read from git diff.",
    )
    return parser.parse_args()


def changed_python_files(base: str, head: str) -> list[Path]:
    head = normalize_revision(head)
    base = normalize_base_revision(base, head)
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", base, head],
        check=True,
        capture_output=True,
        text=True,
    )
    files = [Path(line) for line in result.stdout.splitlines() if line.endswith(".py")]
    return files


def commit_exists(rev: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


@lru_cache(maxsize=1)
def empty_tree_revision() -> str:
    result = subprocess.run(
        ["git", "hash-object", "-t", "tree", "/dev/null"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def normalize_revision(rev: str) -> str:
    if rev == "0000000000000000000000000000000000000000":
        return empty_tree_revision()
    if not commit_exists(rev):
        return empty_tree_revision()
    return rev


def normalize_base_revision(base: str, head: str) -> str:
    if base == "0000000000000000000000000000000000000000":
        return empty_tree_revision()
    if commit_exists(base):
        return base

    parent = f"{head}^1"
    if commit_exists(parent):
        return parent
    return empty_tree_revision()


def compile_files(files: list[Path]) -> None:
    for file_path in files:
        py_compile.compile(str(file_path), doraise=True)


def run_ruff(files: list[Path]) -> None:
    command = [sys.executable, "-m", "ruff", "check", "--select", "F821", *map(str, files)]
    result = subprocess.run(command)
    if result.returncode:
        if result.returncode == 1:
            print("Python undefined-name validation failed, or ruff is not installed.", file=sys.stderr)
        raise SystemExit(result.returncode)


def main() -> int:
    args = parse_args()
    files = [Path(path) for path in args.paths] if args.paths else changed_python_files(args.base, args.head)
    files = [path for path in files if path.suffix == ".py"]

    if not files:
        print("No changed Python files to validate.")
        return 0

    print("Validating Python files:")
    for file_path in files:
        print(f"  {file_path}")

    compile_files(files)
    run_ruff(files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
