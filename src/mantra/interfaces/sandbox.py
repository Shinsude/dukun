"""Sandbox contract: an isolated place where agent commands execute."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ExecResult:
    """Outcome of one command execution inside the sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class Sandbox(ABC):
    """Execution environment for one task run.

    Implementations own their lifecycle: ``setup`` provisions the working
    environment, ``exec`` runs commands, ``cleanup`` releases resources and
    must be safe to call more than once.
    """

    @abstractmethod
    def setup(self, task: dict) -> None:
        """Prepare the environment for the task (repo checkout, deps)."""
        raise NotImplementedError

    @abstractmethod
    def exec(self, command: str, timeout: float = 120.0) -> ExecResult:
        """Run a shell command in the sandbox and capture its output."""
        raise NotImplementedError

    @abstractmethod
    def read_file(self, path: str) -> str:
        """Return file content relative to the sandbox workspace."""
        raise NotImplementedError

    @abstractmethod
    def write_file(self, path: str, content: str) -> None:
        """Write (create or overwrite) a file relative to the sandbox workspace."""
        raise NotImplementedError

    @abstractmethod
    def cleanup(self) -> None:
        """Tear down the environment. Idempotent."""
        raise NotImplementedError
