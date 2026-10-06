"""Shared safe failures for operator commands and artifact writers."""


class AdminError(Exception):
    """A user-facing failure code without database contents or input paths."""
