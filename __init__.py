"""hermes-evolve: hot-reload plugins and evolve the system prompt in-place.

Usage:
  /evolve now    — reload all loaded plugin modules, re-register, clear caches,
                   clear all stored system prompts → forces rebuild on next message
  /evolve status — show loaded plugin modules and what /evolve now will affect

Works across CLI, TUI, Desktop, and Gateway — all modes share the same
state.db, and clearing all stored system_prompts triggers a rebuild on
every active session's next turn.
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes-evolve")


def register(ctx: Any) -> None:
    ctx.register_command(
        "evolve",
        _cmd_evolve,
        description="Hot-reload plugins and rebuild system prompt on next message",
        args_hint="now|status",
    )
    logger.info("hermes-evolve: registered /evolve command")


# ── command dispatch ────────────────────────────────────────


def _cmd_evolve(raw_args: str) -> str | None:
    cmd = raw_args.strip().lower()
    if cmd == "status":
        return _cmd_status()
    if cmd == "now":
        return _cmd_now()
    return "Usage:\n  /evolve now    — hot-reload all plugins and rebuild system prompt\n  /evolve status — show loaded plugins and what will be affected"


# ── status ──────────────────────────────────────────────────


def _cmd_status() -> str:
    loaded = _loaded_plugin_modules()
    lines = ["## Evolve — Status", "", f"{len(loaded)} plugin module(s) loaded:", ""]
    for mod_name, filepath in loaded:
        mtime = ""
        try:
            from datetime import datetime
            mtime = datetime.fromtimestamp(Path(filepath).stat().st_mtime).strftime("%H:%M:%S")
        except Exception:
            pass
        tag = "🔄" if _has_disk_changes(filepath) else "  "
        line = f"  {tag} {mod_name}"
        if mtime:
            line += f"  ({mtime})"
        lines.append(line)

    try:
        from hermes_cli.plugins import get_plugin_manager
        mgr = get_plugin_manager()
        names = sorted(k for k, v in mgr._plugins.items() if v.enabled and v.module)
        lines.append("")
        lines.append(f"{len(names)} plugin(s) will be re-registered:")
        for name in names:
            lines.append(f"    {name}")
    except Exception:
        pass

    return "\n".join(lines) if len(lines) > 3 else "\n".join(lines) + "\n(no plugin modules loaded)"


# ── now ─────────────────────────────────────────────────────


def _cmd_now() -> str:
    parts: list[str] = []
    reloaded = 0

    # ── Step 1: Reload all loaded plugin modules ──
    for mod_name, _ in _loaded_plugin_modules():
        mod = sys.modules.get(mod_name)
        if mod is None or not hasattr(mod, "__file__") or mod.__file__ is None:
            continue
        try:
            importlib.reload(mod)
            reloaded += 1
            parts.append(f"  🔄 {mod_name}")
        except Exception as e:
            parts.append(f"  ❌ {mod_name}: {e}")

    if reloaded == 0:
        parts.append("  (no modules to reload)")

    # ── Step 2: Re-discover + re-register all plugins ──
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins(force=True)
        parts.append("  🔄 plugins re-discovered + re-registered")
    except Exception as e:
        parts.append(f"  ⚠ plugin rediscovery: {e}")

    # ── Step 3: Clear model_tools cache + bump registry generation ──
    try:
        import model_tools
        model_tools._tool_defs_cache.clear()
        registry = getattr(model_tools, "registry", None)
        if registry is not None:
            registry._generation += 1
            parts.append("  🔄 registry._generation bumped")
        parts.append("  🔄 model_tools cache cleared")
    except Exception as e:
        parts.append(f"  ⚠ model_tools: {e}")

    # ── Step 4: Clear ALL stored system prompts ──
    # Works across CLI/TUI/Desktop/Gateway — all share state.db.
    # Empty string triggers _restore_or_build_system_prompt to rebuild.
    cleared = 0
    try:
        from hermes_state import DEFAULT_DB_PATH
        conn = sqlite3.connect(str(DEFAULT_DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.execute("UPDATE sessions SET system_prompt = '' WHERE system_prompt IS NOT NULL AND system_prompt != ''")
        cleared = cur.rowcount
        conn.commit()
        conn.close()
        parts.append(f"  🔄 {cleared} stored system prompt(s) cleared")
    except Exception as e:
        parts.append(f"  ⚠ stored prompts: {e}")

    return (
        f"## Evolve — {reloaded} module(s) reloaded\n\n"
        + "\n".join(parts)
        + f"\n\n✅ {cleared} session(s) will rebuild system prompt on their next message."
    )


# ── module discovery ────────────────────────────────────────


def _loaded_plugin_modules() -> list[tuple[str, str]]:
    plugin_home = str(Path.home() / ".hermes" / "plugins")
    hermes_agent_root = ""
    try:
        import hermes_cli
        hermes_agent_root = str(Path(hermes_cli.__file__).resolve().parent.parent)
    except Exception:
        pass

    result: list[tuple[str, str]] = []
    for mod_name, mod in sorted(sys.modules.items()):
        if not hasattr(mod, "__file__") or mod.__file__ is None:
            continue
        fpath = mod.__file__
        if fpath.startswith(plugin_home):
            result.append((mod_name, fpath))
        elif hermes_agent_root and fpath.startswith(hermes_agent_root):
            if fpath[len(hermes_agent_root):].startswith("/plugins/"):
                result.append((mod_name, fpath))
    return result


def _has_disk_changes(filepath: str) -> bool:
    try:
        import os, time
        return (time.time() - os.path.getmtime(filepath)) < 3600
    except Exception:
        return False
