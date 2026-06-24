"""Hermes directory-plugin entry point for kanban-warden."""

from __future__ import annotations

if __package__:
    from .kanban_warden import register, unregister
else:
    from kanban_warden import register, unregister

__all__ = ["register", "unregister"]
