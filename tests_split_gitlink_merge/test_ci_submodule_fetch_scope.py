import tempfile
import types
import unittest
from pathlib import Path

from ci_merge_split import preflight


class FakeCommandResult:
    def __init__(self, args: list[str], stdout: str = "", stderr: str = "", exit_code: int = 0, cwd: Path | None = None) -> None:
        self.args = args
        self.cmd = ["git", *args]
        self.cwd = str(cwd or Path("repo"))
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, object]:
        return {
            "cmd": self.cmd,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class FakeReport:
    def __init__(self) -> None:
        self.data = {
            "remote_sync": {
                "attempted": False,
                "success": None,
                "remote": "origin",
                "recursive_submodules": True,
                "started_at": None,
                "ended_at": None,
            }
        }


class NeededSubmoduleRunner:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.child = repo / "libs" / "changed"
        self.calls: list[tuple[Path, tuple[str, ...], bool]] = []

    def run(self, repo_path: Path, args: list[str], *, allow_failure: bool = False) -> FakeCommandResult:
        self.calls.append((repo_path, tuple(args), allow_failure))
        stdout = self.stdout_for(repo_path, args)
        return FakeCommandResult(args, stdout=stdout, cwd=repo_path)

    def stdout_for(self, repo_path: Path, args: list[str]) -> str:
        if args == ["rev-parse", "--verify", "refs/remotes/origin/source^{commit}"]:
            return "sourcesha\n"
        if args == ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"]:
            return "targetsha\n"
        if args == ["merge-base", "sourcesha", "targetsha"]:
            return "basesha\n"
        if args == ["diff", "--raw", "--no-abbrev", "basesha", "sourcesha", "--"]:
            return ":160000 160000 basechild sourcechild M\tlibs/changed\n"
        if args == ["diff", "--raw", "--no-abbrev", "basesha", "targetsha", "--"]:
            return ""
        if args == ["ls-tree", "basesha", "--", "libs/changed"]:
            return "160000 commit basechild\tlibs/changed\n"
        if args == ["ls-tree", "sourcesha", "--", "libs/changed"]:
            return "160000 commit sourcechild\tlibs/changed\n"
        if args == ["ls-tree", "targetsha", "--", "libs/changed"]:
            return "160000 commit basechild\tlibs/changed\n"
        if repo_path == self.child and args == ["rev-parse", "--is-inside-work-tree"]:
            return "true\n"
        return ""


class FallbackSubmoduleRunner(NeededSubmoduleRunner):
    def stdout_for(self, repo_path: Path, args: list[str]) -> str:
        if args == ["diff", "--raw", "--no-abbrev", "basesha", "sourcesha", "--"]:
            return ":100644 100644 old new M\t.gitmodules\n"
        if args == ["submodule", "status", "--recursive"]:
            return " 1111111 libs/one (heads/main)\n 2222222 libs/two (heads/main)\n"
        if repo_path in {self.repo / "libs" / "one", self.repo / "libs" / "two"} and args == ["rev-parse", "--is-inside-work-tree"]:
            return "true\n"
        return super().stdout_for(repo_path, args)


def make_context(runner: object, *, scope: str = "needed") -> object:
    return types.SimpleNamespace(
        args=types.SimpleNamespace(
            source="source",
            sources=["source"],
            target="main",
            remote="origin",
            submodule_fetch_scope=scope,
            max_depth=16,
        ),
        max_depth=16,
        runner=runner,
        report=FakeReport(),
        set_state=lambda _state: None,
    )


class CiSubmoduleFetchScopeTests(unittest.TestCase):
    def test_needed_scope_fetches_only_changed_gitlink_submodule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            runner = NeededSubmoduleRunner(repo)
            ctx = make_context(runner)

            preflight.sync_remote_refs(ctx, repo)

            called_args = [call[1] for call in runner.calls]
            fetch_repos = [call[0] for call in runner.calls if call[1] == ("fetch", "--all", "--prune")]
            self.assertEqual(fetch_repos, [repo / "libs" / "changed"])
            self.assertNotIn(("submodule", "foreach", "--recursive", "git fetch --all --prune"), called_args)
            self.assertEqual(ctx.report.data["remote_sync"]["submodule_fetch_scope_effective"], "needed")
            self.assertEqual(ctx.report.data["remote_sync"]["submodule_fetch_needed_paths"], ["libs/changed"])

    def test_needed_scope_falls_back_to_all_for_gitmodules_change_without_foreach(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            runner = FallbackSubmoduleRunner(repo)
            ctx = make_context(runner)

            preflight.sync_remote_refs(ctx, repo)

            called_args = [call[1] for call in runner.calls]
            fetch_repos = [call[0] for call in runner.calls if call[1] == ("fetch", "--all", "--prune")]
            self.assertEqual(fetch_repos, [repo / "libs" / "one", repo / "libs" / "two"])
            self.assertNotIn(("submodule", "foreach", "--recursive", "git fetch --all --prune"), called_args)
            self.assertEqual(ctx.report.data["remote_sync"]["submodule_fetch_scope_effective"], "all")
            self.assertEqual(ctx.report.data["remote_sync"]["submodule_fetch_fallback_reasons"], ["GITMODULES_CHANGED"])


if __name__ == "__main__":
    unittest.main()
