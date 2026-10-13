"""Errors and stable exit codes (design §10: exit codes are part of the interface)."""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2  # click's own usage-error code
EXIT_NEEDS_ROOT = 3
EXIT_CHECKS_FAILED = 4
EXIT_CONFLICT = 5


class HitkitError(Exception):
    exit_code = EXIT_ERROR

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class NeedsRoot(HitkitError):
    exit_code = EXIT_NEEDS_ROOT


class Conflict(HitkitError):
    """Existing state that hitkit will not touch without an explicit override."""

    exit_code = EXIT_CONFLICT
