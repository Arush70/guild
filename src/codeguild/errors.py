"""Exceptions that cross module boundaries."""

from __future__ import annotations


class Cancelled(BaseException):
    """The owner cancelled the running job (dashboard / VS Code "stop", Ctrl+C in `codeguild pair`).

    A BaseException on purpose: provider adapters and tools catch `Exception` to turn failures
    into fallbacks or tool errors, and a cancel must pass straight through all of them."""
