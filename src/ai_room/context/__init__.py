"""Environment-backed context adapters for interactive AI sessions."""

from .base import (
    ContextAdapter,
    SessionDetectionError,
    SessionIdentity,
    adapter_for,
    detect_current_session,
)

__all__ = [
    "ContextAdapter",
    "SessionDetectionError",
    "SessionIdentity",
    "adapter_for",
    "detect_current_session",
]
