"""hermes-evolve: hot-reload plugins and evolve the system prompt in-place.

Usage:
  /evolve now    — reload all loaded plugin modules, re-register, clear caches,
                   force system prompt rebuild on next message
  /evolve status — show loaded plugins and what /evolve now will affect
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes-evolve")


def register(ctx: Any) -> None:
    """Register /evolve command."""
    ctx.register_command(
        "evolve",
        _cmd_evolve,
        description="Hot-reload plugins and rebuild system prompt on next message",
        args_hint="now|status",
    )
    logger.info("hermes-evolve: registered /evolve command")


# ── command dispatch ────────────────────────────────────────


def _cmd_evolve(raw_args: str) -> str | None:
    """Dispatch: /evolve now | /evolve status"""
    cmd = raw_args.strip().lower()
    if cmd == "status":
        return _cmd_status()
    if cmd == "now":
        return _cmd_now()
    return "Usage:\n  /evolve now    — hot-reload all plugins and trigger system prompt rebuild\n  /evolve status — show loaded plugins and what will be affected"


# ── status ──────────────────────────────────────────────────


def _cmd_status() -> str:
    """Show all loaded plugin modules that /evolve now would reload."""
    loaded = _loaded_plugin_modules()
    lines = ["## Evolve — Status", "", f"{len(loaded)} plugin module(s) loaded:", ""]
    for mod_name, filepath in loaded:
        mtime = ""
        try:
            mtime = Path(filepath).stat().st_mtime
            from datetime import datetime
            mtime = datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
        except Exception:
            pass
        prefix = "🔄" if _has_disk_changes(mod_name, filepath) else "  "
        lines.append(f"  {prefix} {mod_name}  ({mtime})" if mtime else f"  {prefix} {mod_name}")

    # Also show which plugins would be re-registered
    try:
        from hermes_cli.plugins import get_plugin_manager
        mgr = get_plugin_manager()
        plugin_names = [k for k, v in mgr._plugins.items() if v.enabled and v.module]
        lines.append("")
        lines.append(f"{len(plugin_names)} plugin(s) will be re-registered:")
        for name in sorted(plugin_names):
            lines.append(f"    {name}")
    except Exception:
        pass

    return "\n".join(lines) if len(lines) > 3 else "\n".join(lines) + "\n(no plugin modules loaded)"


# ── now ─────────────────────────────────────────────────────


def _cmd_now() -> str:
    """Full reload cycle."""
    parts: list[str] = []
    reloaded = 0

    # ── Step 1: Reload ALL loaded plugin modules ──
    for mod_name, _filepath in _loaded_plugin_modules():
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

    # ── Step 4: Clear in-memory cached system prompt ──
    cleared_memory = False
    try:
        from hermes_cli.plugins import get_plugin_manager
        mgr = get_plugin_manager()
        cli = getattr(mgr, "_cli_ref", None)
        if cli is not None and hasattr(cli, "agent") and cli.agent is not None:
            cli.agent._cached_system_prompt = None
            cleared_memory = True
            parts.append("  🔄 agent._cached_system_prompt cleared")
    except Exception:
        pass

    # ── Step 5: Clear stored system prompt from session DB ──
    if cleared_memory:
        try:
            from hermes_cli.plugins import get_plugin_manager
            from hermes_state import SessionDB
            cli = getattr(get_plugin_manager(), "_cli_ref", None)
            if cli is not None and hasattr(cli, "agent") and cli.agent is not None:
                sid = cli.agent.session_id
                if sid:
                    db = SessionDB()
                    db.update_system_prompt(sid, "")
                    parts.append(f"  🔄 session DB system_prompt cleared ({sid[:12]}...)")
        except Exception as e:
            parts.append(f"  ⚠ session DB: {e}")

    if not cleared_memory:
        parts.append("  ⚠ Could not clear cached prompt. Use /new to rebuild.")

    return (
        f"## Evolve — {reloaded} module(s) reloaded\n\n"
        + "\n".join(parts)
        + "\n\n✅ System prompt will rebuild on your next message."
    )


# ── module discovery ────────────────────────────────────────


def _loaded_plugin_modules() -> list[tuple[str, str]]:
    """Return (module_name, filepath) for all loaded plugin modules.

    Covers both user plugins (~/.hermes/plugins/) and bundled plugins
    (<repo>/plugins/), excluding built-in and stdlib.
    """
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

        # User plugins: under ~/.hermes/plugins/
        if fpath.startswith(plugin_home):
            result.append((mod_name, fpath))
            continue

        # Bundled plugins: under <repo>/plugins/<name>/ (skip core agent modules)
        if hermes_agent_root and fpath.startswith(hermes_agent_root):
            rel = fpath[len(hermes_agent_root):]
            if rel.startswith("/plugins/"):
                result.append((mod_name, fpath))
                continue

    return result


def _has_disk_changes(mod_name: str, filepath: str) -> bool:
    """Check if module has been modified on disk since import."""
    try:
        import os
        mod = sys.modules.get(mod_name)
        if mod is None:
            return False
        disk_mtime = os.path.getmtime(filepath)
        # Python doesn't store import time, so we approximate: any file
        # modified in the last hour is potentially changed
        import time
        return (time.time() - disk_mtime) < 3600
    except Exception:
        return False
