"""Stable command exit codes and actionable failure classes."""

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    CONFIGURATION = 2
    PREPARATION = 3
    COLLECTION = 4
    RECONCILIATION = 5
    INTERRUPTED = 130


class CompareError(Exception):
    exit_code = ExitCode.CONFIGURATION


class IntegrityError(CompareError):
    """An immutable artifact no longer matches its identity."""


class PreparationError(CompareError):
    exit_code = ExitCode.PREPARATION


class ReconciliationError(CompareError):
    exit_code = ExitCode.RECONCILIATION
