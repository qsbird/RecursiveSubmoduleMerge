"""Create test branches with submodule file conflicts in the qemu repo."""
import subprocess, os, sys

repo = r"D:\Git\MergeTool_for_CICD\external_builds\qemu"

def git(args, cwd=repo, check=False):
    r = subprocess.run(["git", "-c", "safe.directory=*"] + args, cwd=cwd,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"FAIL: git {' '.join(args)}")
        print(r.stderr)
        sys.exit(1)
    return r.stdout.strip(), r.stderr.strip(), r.returncode

print("=== Step 1: Create test branch from master ===")
git(["checkout", "master"])
submod_paths_out, _, _ = git(["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"])
submod_paths = [line.split(maxsplit=1)[1].strip() for line in submod_paths_out.splitlines() if line.strip()]
print(f"Found {len(submod_paths)} submodules in .gitmodules")

git(["branch", "-D", "test-merge-conflict"], check=False)
git(["checkout", "-b", "test-merge-conflict"], check=True)

print("\n=== Step 2: Initialize first 6 submodules ===")
targets = submod_paths[:6]
for sp in targets:
    print(f"  Init: {sp}")
    git(["submodule", "update", "--init", sp], check=False)

print("\n=== Step 3: Add test files inside submodules (test-branch version) ===")
for sp in targets[:4]:
    sub_dir = os.path.join(repo, sp)
    if not os.path.exists(sub_dir):
        print(f"  SKIP {sp}: dir not found")
        continue
    test_file = os.path.join(sub_dir, "merge_test_conflict.txt")
    with open(test_file, "w") as f:
        f.write(f"// TEST-BRANCH VERSION of merge test in {sp}\n")
        f.write("int main() {\n")
        f.write('    printf("hello from test branch");\n')
        f.write("    return 0;\n")
        f.write("}\n")
    print(f"  Created: {sp}/merge_test_conflict.txt")
    git(["add", "merge_test_conflict.txt"], cwd=sub_dir, check=False)
    git(["commit", "-m", "test: add merge test file (test-branch version)"], cwd=sub_dir, check=False)

print("\n=== Step 4: Commit gitlink updates on test-branch ===")
for sp in targets[:4]:
    git(["add", sp], check=False)
git(["commit", "-m", "test-branch: update submodule gitlinks for merge test"], check=False)
test_sha, _, _ = git(["rev-parse", "HEAD"])
print(f"Test branch HEAD: {test_sha[:12]}")

print("\n=== Step 5: Switch to master and make conflicting changes ===")
git(["checkout", "master"], check=True)
git(["submodule", "update", "--init"] + targets[:4], check=False)
for sp in targets[:4]:
    sub_dir = os.path.join(repo, sp)
    if not os.path.exists(sub_dir):
        print(f"  SKIP {sp}: dir not found")
        continue
    test_file = os.path.join(sub_dir, "merge_test_conflict.txt")
    with open(test_file, "w") as f:
        f.write(f"// MASTER VERSION of merge test in {sp}\n")
        f.write("int main() {\n")
        f.write('    printf("hello from master branch!");\n')
        f.write('    printf("this extra line only exists on master");\n')
        f.write("    return 0;\n")
        f.write("}\n")
    print(f"  Modified: {sp}/merge_test_conflict.txt")
    git(["add", "merge_test_conflict.txt"], cwd=sub_dir, check=False)
    git(["commit", "-m", "test: modify merge test file (master version)"], cwd=sub_dir, check=False)

print("\n=== Step 6: Commit gitlink updates on master ===")
for sp in targets[:4]:
    git(["add", sp], check=False)
git(["commit", "-m", "master: update submodule gitlinks with master changes"], check=False)
master_sha, _, _ = git(["rev-parse", "HEAD"])
print(f"Master HEAD: {master_sha[:12]}")

# Create another test branch from the test-merge-conflict for merge testing
print("\n=== Step 7: Create merge target ===")
git(["checkout", "test-merge-conflict"], check=True)
git(["checkout", "-b", "merge-target-test"], check=False)

# Switch back to master
git(["checkout", "master"], check=True)

print("\n=== DONE ===")
print(f"Branches created:")
print(f"  master            -> {master_sha[:12]}")
print(f"  test-merge-conflict -> {test_sha[:12]}")
print(f"  merge-target-test -> (same as test-merge-conflict)")
print()
print(f"To test with manual merge tool:")
print(f"  Merge test-merge-conflict into master:")
print(f"  {sys.argv[0]} replacement: py -m manual_merge_split merge test-merge-conflict --repo {repo} --continue-current-branch")