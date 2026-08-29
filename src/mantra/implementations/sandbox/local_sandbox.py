"""Sandbox that executes directly in a host working directory.

No isolation: intended for trusted local development and testing.
The Docker sandbox is the isolated counterpart for real agent runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from mantra.core.exceptions import AbortError, SandboxError
from mantra.interfaces.sandbox import ExecResult, Sandbox


class LocalSandbox(Sandbox):
    """Runs commands in a scratch directory on the host."""

    def __init__(self, workspace_root: str | None = None) -> None:
        self._root = workspace_root
        self._owns_root = workspace_root is None
        self.changed: set[str] = set()  # paths written this session

    def setup(self, task: dict) -> None:
        if self._root is None:
            self._root = tempfile.mkdtemp(prefix="mantra-task-")
        os.makedirs(self._root, exist_ok=True)

        repo_url = task.get("repo_url")
        if repo_url:
            if not self._is_safe_repo_url(str(repo_url)):
                raise SandboxError(f"repo_url rejected: {repo_url!r}")
            result = self._exec_no_shell(
                ["git", "clone", str(repo_url), "."],
                timeout=task.get("clone_timeout", 300),
            )
            if result.exit_code != 0:
                raise SandboxError(
                    f"git clone failed ({result.exit_code}): {result.stderr[:2000]}"
                )
            commit = task.get("base_commit")
            if commit:
                if not self._is_safe_commit(str(commit)):
                    raise SandboxError(f"base_commit rejected: {commit!r}")
                result = self._exec_no_shell(
                    ["git", "checkout", str(commit)], timeout=60
                )
                if result.exit_code != 0:
                    raise SandboxError(
                        f"git checkout failed ({result.exit_code}): {result.stderr[:2000]}"
                    )

        setup_cmd = task.get("setup_cmd")
        if setup_cmd:
            result = self.exec(setup_cmd, timeout=task.get("setup_timeout", 600))
            if result.exit_code != 0:
                raise SandboxError(
                    f"setup_cmd failed ({result.exit_code}): {result.stderr[:2000]}"
                )

    @property
    def root(self) -> str:
        if self._root is None:
            raise SandboxError("sandbox not set up")
        return self._root

    def exec(self, command: str, timeout: float = 120.0) -> ExecResult:
        abort = getattr(self, "abort", None)
        if abort is not None and abort.is_set():
            raise AbortError("interrupted by operator")
        # Use Popen so abort can interrupt a long-running command.
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
            elapsed = 0.0
            interval = 0.1
            while True:
                if abort is not None and abort.is_set():
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                    except OSError:
                        pass
                    raise AbortError("interrupted by operator")
                try:
                    stdout, stderr = proc.communicate(timeout=interval)
                    return ExecResult(
                        exit_code=proc.returncode,
                        stdout=stdout or "",
                        stderr=stderr or "",
                    )
                except subprocess.TimeoutExpired:
                    elapsed += interval
                    if elapsed >= timeout:
                        try:
                            proc.kill()
                            stdout, stderr = proc.communicate(timeout=2)
                        except Exception:
                            stdout, stderr = "", ""
                        return ExecResult(
                            exit_code=-1,
                            stdout=stdout or "",
                            stderr=stderr or "",
                            timed_out=True,
                        )
                    continue
        except OSError as exc:
            return ExecResult(exit_code=-1, stdout="", stderr=str(exc), timed_out=False)

    def read_file(self, path: str) -> str:
        full = self._resolve(path)
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def write_file(self, path: str, content: str) -> None:
        full = self._resolve(path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        self.changed.add(path.replace("\\", "/"))

    def cleanup(self) -> None:
        """Release resources. Idempotent; safe for long-lived sandboxes.

        Only a sandbox that created its own scratch directory forgets its
        root. A sandbox handed a workspace (the console case) keeps pointing
        at it, so a second run cannot silently drift into a temp directory.
        """
        if self._owns_root and self._root and os.path.isdir(self._root):
            shutil.rmtree(self._root, ignore_errors=True)
            self._root = None

    def _resolve(self, path: str) -> str:
        """Join path onto the workspace and refuse escapes, resolving symlinks."""
        full = os.path.realpath(os.path.join(self.root, path))
        base = os.path.realpath(self.root)
        if not (full == base or full.startswith(base + os.sep)):
            raise SandboxError(f"path escapes sandbox workspace: {path}")
        return full

    def _exec_no_shell(self, args: list[str], timeout: float = 120.0) -> ExecResult:
        """Run a command without shell interpretation, abort-aware."""
        abort = getattr(self, "abort", None)
        if abort is not None and abort.is_set():
            raise AbortError("interrupted by operator")
        try:
            proc = subprocess.Popen(
                args,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
            elapsed = 0.0
            interval = 0.1
            while True:
                if abort is not None and abort.is_set():
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                    except OSError:
                        pass
                    raise AbortError("interrupted by operator")
                try:
                    stdout, stderr = proc.communicate(timeout=interval)
                    return ExecResult(
                        exit_code=proc.returncode,
                        stdout=stdout or "",
                        stderr=stderr or "",
                    )
                except subprocess.TimeoutExpired:
                    elapsed += interval
                    if elapsed >= timeout:
                        try:
                            proc.kill()
                            stdout, stderr = proc.communicate(timeout=2)
                        except Exception:
                            stdout, stderr = "", ""
                        return ExecResult(
                            exit_code=-1,
                            stdout=stdout or "",
                            stderr=stderr or "",
                            timed_out=True,
                        )
                    continue
        except OSError as exc:
            return ExecResult(exit_code=-1, stdout="", stderr=str(exc), timed_out=False)

    @staticmethod
    def _is_safe_repo_url(url: str) -> bool:
        url = url.strip()
        if not url or len(url) > 2048 or "\n" in url or "\r" in url or "\x00" in url:
            return False
        if url.startswith(("http://", "https://", "git@", "ssh://", "git://")):
            return True
        # allow local file paths for tests
        if url.startswith("file://"):
            return True
        return False

    @staticmethod
    def _is_safe_commit(commit: str) -> bool:
        commit = commit.strip()
        if not commit or len(commit) > 256 or "\n" in commit or "\r" in commit or "\x00" in commit:
            return False
        # block shell metacharacters
        if any(c in commit for c in (";", "&", "|", "`", "$", "(", ")", "<", ">", '"', "'")):
            return False
        return True
