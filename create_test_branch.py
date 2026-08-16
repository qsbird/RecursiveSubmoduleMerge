"""Create test branches with submodule file conflicts in a target repo."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


REPO_ENV_VAR = "MERGETOOL_TEST_BRANCH_REPO"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create merge-conflict test branches in a target repository.")
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help=f"target repository path; defaults to ${REPO_ENV_VAR} when set",
    )
    args = parser.parse_args()
    repo = args.repo or (Path(os.environ[REPO_ENV_VAR]) if REPO_ENV_VAR in os.environ else None)
    if repo is None:
        parser.error(f"--repo is required unless ${REPO_ENV_VAR} is set")
    args.repo = repo.expanduser().resolve()
    return args


def git(args: list[str], repo: Path, cwd: Path | None = None, check: bool = False) -> tuple[str, str, int]:
    run_cwd = cwd or repo
    result = subprocess.run(
        ["git", "-c", "safe.directory=*"] + args,
        cwd=run_cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        print(f"FAIL: git {' '.join(args)}")
        print(result.stderr)
        sys.exit(1)
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def main() -> int:
    repo = parse_args().repo

    print("=== Step 1: Create test branch from master ===")
    git(["checkout", "master"], repo)
    submod_paths_out, _, _ = git(["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"], repo)
    submod_paths = [line.split(maxsplit=1)[1].strip() for line in submod_paths_out.splitlines() if line.strip()]
    print(f"Found {len(submod_paths)} submodules in .gitmodules")

    git(["branch", "-D", "test-merge-conflict"], repo, check=False)
    git(["checkout", "-b", "test-merge-conflict"], repo, check=True)

    print("\n=== Step 2: Initialize first 6 submodules ===")
    targets = submod_paths[:6]
    for sp in targets:
        print(f"  Init: {sp}")
        git(["submodule", "update", "--init", sp], repo, check=False)

    print("\n=== Step 3: Add test files inside submodules (test-branch version) ===")
    for sp in targets[:4]:
        sub_dir = repo / sp
        if not sub_dir.exists():
            print(f"  SKIP {sp}: dir not found")
            continue
        test_file = sub_dir / "merge_test_conflict.txt"
        test_file.write_text(
            "\n".join(
                [
                    f"// TEST-BRANCH VERSION of merge test in {sp}",
                    "int main() {",
                    '    printf("hello from test branch");',
                    "    return 0;",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(f"  Created: {sp}/merge_test_conflict.txt")
        git(["add", "merge_test_conflict.txt"], repo, cwd=sub_dir, check=False)
        git(["commit", "-m", "test: add merge test file (test-branch version)"], repo, cwd=sub_dir, check=False)

    print("\n=== Step 4: Commit gitlink updates on test-branch ===")
    for sp in targets[:4]:
        git(["add", sp], repo, check=False)
    git(["commit", "-m", "test-branch: update submodule gitlinks for merge test"], repo, check=False)
    test_sha, _, _ = git(["rev-parse", "HEAD"], repo)
    print(f"Test branch HEAD: {test_sha[:12]}")

    print("\n=== Step 5: Switch to master and make conflicting changes ===")
    git(["checkout", "master"], repo, check=True)
    git(["submodule", "update", "--init"] + targets[:4], repo, check=False)
    for sp in targets[:4]:
        sub_dir = repo / sp
        if not sub_dir.exists():
            print(f"  SKIP {sp}: dir not found")
            continue
        test_file = sub_dir / "merge_test_conflict.txt"
        test_file.write_text(
            "\n".join(
                [
                    f"// MASTER VERSION of merge test in {sp}",
                    "int main() {",
                    '    printf("hello from master branch!");',
                    '    printf("this extra line only exists on master");',
                    "    return 0;",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(f"  Modified: {sp}/merge_test_conflict.txt")
        git(["add", "merge_test_conflict.txt"], repo, cwd=sub_dir, check=False)
        git(["commit", "-m", "test: modify merge test file (master version)"], repo, cwd=sub_dir, check=False)

    print("\n=== Step 6: Commit gitlink updates on master ===")
    for sp in targets[:4]:
        git(["add", sp], repo, check=False)
    git(["commit", "-m", "master: update submodule gitlinks with master changes"], repo, check=False)
    master_sha, _, _ = git(["rev-parse", "HEAD"], repo)
    print(f"Master HEAD: {master_sha[:12]}")

    print("\n=== Step 7: Create merge target ===")
    git(["checkout", "test-merge-conflict"], repo, check=True)
    git(["checkout", "-b", "merge-target-test"], repo, check=False)
    git(["checkout", "master"], repo, check=True)

    print("\n=== DONE ===")
    print("Branches created:")
    print(f"  master              -> {master_sha[:12]}")
    print(f"  test-merge-conflict -> {test_sha[:12]}")
    print("  merge-target-test   -> (same as test-merge-conflict)")
    print()
    print("To test with manual merge tool:")
    print("  Merge test-merge-conflict into master:")
    print(
        f"  {Path(sys.argv[0]).name} replacement: "
        f"py -m manual_merge_split merge test-merge-conflict --repo {repo} --continue-current-branch"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
