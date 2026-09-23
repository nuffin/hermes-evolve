"""Three-state, dual-backend stored system-prompt invalidation.

The `/evolve` command must clear stored system-prompt snapshots so sessions
rebuild them on next run. Storage lives in ONE of:

  state A  -- stock upstream core: no ``clear_stored_system_prompts`` anywhere.
              Raw SQLite fallback over ``<home>/state.db`` (both layouts).
  state B  -- state-store interface line (SQLite or PostgreSQL): the selected
              CLI session store exposes ``clear_stored_system_prompts()``.

Because SQLite and PostgreSQL backends can be switched between (and a profile
may hold leftover state.db rows after a switch), the two cleanups are
INDEPENDENT checks, not an if/else: clear the selected store via the interface
method when present, AND clear a real ``state.db`` file via raw SQLite when one
exists. Either leg may be skipped with an honest report; neither failure aborts
the other.
"""

from __future__ import annotations

import sqlite3


def clear_stored_prompts_report() -> tuple[int, str, list[str]]:
    """Invalidate stored prompts on every backend that actually holds them.

    Returns ``(total_cleared, storage_mode, lines)`` where *lines* are
    human-readable per-leg outcomes for the `/evolve` report.
    """
    total_cleared = 0
    modes: list[str] = []
    lines: list[str] = []

    # ── Leg 1: selected session store (interface line, SQLite or PG) ──
    interface = None
    interface_error = None
    try:
        interface = _clear_via_interface()
    except InterfaceClearError as exc:
        interface_error = str(exc)
    if interface_error is not None:
        lines.append(f"  ⚠ selected store: {interface_error}")
    elif interface is None:
        lines.append("  ℹ selected store: no clear_stored_system_prompts capability (stock core)")
    else:
        cleared, mode = interface
        total_cleared += cleared
        modes.append(mode)
        lines.append(f"  🔄 selected store: {cleared} snapshot reference(s) invalidated ({mode})")

    # ── Leg 2: profile state.db (raw SQLite; catches switch leftovers) ──
    database_path = _state_db_path()
    if database_path is None:
        lines.append("  ℹ state.db: not present (nothing to clear)")
    else:
        try:
            cleared, mode = _clear_stored_system_prompts_sqlite(database_path)
            total_cleared += cleared
            modes.append(mode)
            lines.append(f"  🔄 state.db: {cleared} snapshot reference(s) invalidated ({mode})")
        except Exception as exc:  # noqa: BLE001 - report, never abort the command
            lines.append(f"  ⚠ state.db: {exc}")

    mode = " + ".join(modes) if modes else "none"
    return total_cleared, mode, lines


def _clear_via_interface() -> tuple[int, str] | None:
    """Clear through the selected CLI session store's interface method.

    ``None`` means the capability does not exist (stock upstream core) — the
    caller falls back to raw SQLite only. A store that EXISTS but fails to
    open raises :class:`InterfaceClearError` so the report can surface the real
    failure instead of silently masquerading as a stock core.
    """
    try:
        from cli import CLI_CONFIG
        from cli_session_store import open_cli_session_store
    except ImportError:
        # Off the interface line entirely (no cli_session_store module) —
        # genuinely stock, or running outside a Hermes process.
        return None
    try:
        store = open_cli_session_store(CLI_CONFIG, read_only=False)
    except Exception as exc:
        raise InterfaceClearError(f"selected store failed to open: {exc}") from exc
    method = getattr(store, "clear_stored_system_prompts", None)
    if not callable(method):
        return None
    try:
        result = method()
    except Exception as exc:
        raise InterfaceClearError(f"clear_stored_system_prompts failed: {exc}") from exc
    if isinstance(result, dict):
        return int(result.get("cleared", 0)), str(result.get("storage_mode", "unknown"))
    return (0, "unknown")


class InterfaceClearError(RuntimeError):
    """The interface-line store exists but its clear leg failed."""


def _state_db_path():
    try:
        from hermes_state import DEFAULT_DB_PATH
        from pathlib import Path
        path = Path(DEFAULT_DB_PATH)
        return path if path.exists() else None
    except Exception:
        return None


def _clear_stored_system_prompts_sqlite(database_path) -> tuple[int, str]:
    """Raw-SQLite invalidation for either legacy layout (unchanged behavior)."""
    conn = sqlite3.connect(str(database_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        if "system_prompt_hash" in columns:
            cur = conn.execute(
                "UPDATE sessions "
                "SET system_prompt = NULL, system_prompt_hash = NULL "
                "WHERE system_prompt_hash IS NOT NULL "
                "OR (system_prompt IS NOT NULL AND system_prompt != '')"
            )
            has_prompt_store = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'system_prompts'"
            ).fetchone()
            if has_prompt_store:
                conn.execute(
                    "DELETE FROM system_prompts "
                    "WHERE NOT EXISTS ("
                    "SELECT 1 FROM sessions "
                    "WHERE sessions.system_prompt_hash = system_prompts.hash"
                    ")"
                )
            storage_mode = "hash-backed"
        elif "system_prompt" in columns:
            cur = conn.execute(
                "UPDATE sessions SET system_prompt = '' "
                "WHERE system_prompt IS NOT NULL AND system_prompt != ''"
            )
            storage_mode = "inline"
        else:
            conn.commit()
            return 0, "none"
        conn.commit()
        return cur.rowcount, storage_mode
    finally:
        conn.close()
