#!/usr/bin/env python3
"""
Portable Cross-Platform Path & State Resolution (`paths.py`).

Eliminates every hardcoded machine/user path across Windows, macOS, and Linux/WSL.
Resolves kit assets from `$AI_KIT_HOME` or package installation directory, and runtime
state/locks/ledger from `$AI_KIT_STATE_DIR` (default `~/.kaizen-kit`).
"""

import os
from pathlib import Path


def resolve_kit_root() -> Path:
    """Return the root directory containing `assets/` (agents, rules, skills, mcp)."""
    env_home = os.environ.get("AI_KIT_HOME", "").strip()
    if env_home:
        candidate = Path(env_home).expanduser().resolve()
        if (candidate / "assets").is_dir():
            return candidate
        if (candidate / "src" / "kaizen_kit" / "assets").is_dir():
            return candidate / "src" / "kaizen_kit"

    pkg_dir = Path(__file__).resolve().parent
    if (pkg_dir / "assets").is_dir():
        return pkg_dir

    repo_root = pkg_dir.parent.parent
    if (repo_root / "assets").is_dir():
        return repo_root
    return pkg_dir


def resolve_assets_dir() -> Path:
    root = resolve_kit_root()
    return root / "assets"


def resolve_state_dir() -> Path:
    """Return mutable per-developer state directory (`~/.kaizen-kit` or `$AI_KIT_STATE_DIR`)."""
    env_state = os.environ.get("AI_KIT_STATE_DIR", "").strip()
    if env_state:
        p = Path(env_state).expanduser().resolve()
    else:
        p = Path.home() / ".kaizen-kit"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_repo_locks_dir() -> Path:
    """
    Shared cross-tool worktree mutex directory.
    Uses `$AI_KIT_LOCKS_DIR` if set; if `~/.gemini/antigravity/repo_locks` exists alongside
    Claude Code, falls back cleanly or shares `~/.kaizen-kit/repo_locks`.
    """
    env_locks = os.environ.get("AI_KIT_LOCKS_DIR", "").strip()
    if env_locks:
        p = Path(env_locks).expanduser().resolve()
    else:
        p = resolve_state_dir() / "repo_locks"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_ledger_path() -> Path:
    """`$AI_KIT_LEDGER_NAME` lets Claude share the Antigravity ledger."""
    ledger_name = os.environ.get("AI_KIT_LEDGER_NAME", "").strip() or "kaizen_ledger.json"
    return resolve_state_dir() / ledger_name


def resolve_dead_ends_path() -> Path:
    return resolve_state_dir() / "DEAD_ENDS.md"


def resolve_session_state_dir() -> Path:
    p = resolve_state_dir() / "session_state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_vault_path() -> Path:
    p = resolve_state_dir() / "vault"
    p.mkdir(parents=True, exist_ok=True)
    return p / "anonymizer_vault.json"


def resolve_audit_log_path() -> Path:
    return resolve_state_dir() / "kaizen_guard_audit.log"
