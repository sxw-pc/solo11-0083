"""Public configuration façade.

The implementation is split by responsibility so every module stays small:
``config_util`` handles primitives, ``config_screen`` screen contracts and
``config_session`` session navigation scripts.
"""

from __future__ import annotations

from pathlib import Path

from .config_screen import NORMALIZE_RULES, load_screen, load_screens_dir
from .config_types import Column, Expectation, ListRegion, Locator, PagingConfig, RetryConfig, ScreenBatch, ScreenContract, ScreenField, SessionContract, SessionStep
from .config_util import is_loopback_host, load_yaml_document, validate_target_parts
from .errors import E_CONFIG_BAD_LOCATOR, E_CONFIG_BAD_TARGET, E_CONFIG_DUPLICATE_FIELD, E_CONFIG_PARSE, E_CONFIG_SCHEMA, HarvestError

def load_session(path: str | Path) -> SessionContract:
    """Load a session script (delegated to keep this façade import-light)."""
    from .config_session import load_session as _load_session

    return _load_session(path)

def load_screens(path: str | Path) -> dict[str, ScreenContract]:
    return load_screens_dir(path)

__all__ = [
    "NORMALIZE_RULES",
    "Column",
    "Expectation",
    "ListRegion",
    "Locator",
    "PagingConfig",
    "RetryConfig",
    "ScreenBatch",
    "ScreenContract",
    "ScreenField",
    "SessionContract",
    "SessionStep",
    "E_CONFIG_BAD_LOCATOR",
    "E_CONFIG_BAD_TARGET",
    "E_CONFIG_DUPLICATE_FIELD",
    "E_CONFIG_PARSE",
    "E_CONFIG_SCHEMA",
    "HarvestError",
    "is_loopback_host",
    "load_screen",
    "load_screens",
    "load_screens_dir",
    "load_session",
    "load_yaml_document",
    "validate_target_parts",
]
