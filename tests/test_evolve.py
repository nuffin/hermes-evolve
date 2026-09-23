"""Regression coverage for persisted system-prompt invalidation."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "__init__.py"


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("hermes_evolve_test", SOURCE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def load_invalidation_module():
    source = Path(__file__).resolve().parents[1] / "prompt_invalidation.py"
    spec = importlib.util.spec_from_file_location("hermes_evolve_invalidation_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class StoredPromptInvalidationTests(unittest.TestCase):
    def test_command_reports_hash_backed_invalidation(self):
        module = load_plugin_module()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    system_prompt TEXT,
                    system_prompt_hash TEXT
                );
                CREATE TABLE system_prompts (
                    hash TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL
                );
                INSERT INTO sessions VALUES ('hashed', NULL, 'hash-a');
                INSERT INTO system_prompts VALUES ('hash-a', 'stored prompt');
                """
            )
            connection.commit()
            connection.close()

            hermes_state = types.ModuleType("hermes_state")
            setattr(hermes_state, "DEFAULT_DB_PATH", database_path)
            plugins_package = types.ModuleType("hermes_cli")
            setattr(plugins_package, "__path__", [])
            plugins_module = types.ModuleType("hermes_cli.plugins")
            setattr(plugins_module, "discover_plugins", lambda force: None)
            model_tools = types.ModuleType("model_tools")
            setattr(model_tools, "_tool_defs_cache", {"cached": object()})
            setattr(model_tools, "registry", types.SimpleNamespace(_generation=7))
            setattr(module, "_loaded_plugin_modules", lambda: [])

            with patch.dict(
                sys.modules,
                {
                    "hermes_state": hermes_state,
                    "hermes_cli": plugins_package,
                    "hermes_cli.plugins": plugins_module,
                    "model_tools": model_tools,
                },
            ):
                result = module._cmd_now()

            self.assertIn("state.db: 1 snapshot reference(s) invalidated (hash-backed)", result)
            self.assertIn("stock core", result)
            self.assertIn("will rebuild when their sessions next run or resume", result)
            self.assertEqual(model_tools._tool_defs_cache, {})
            self.assertEqual(model_tools.registry._generation, 8)

    def test_hash_backed_schema_clears_hashes_and_orphans(self):
        module = load_plugin_module()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    system_prompt TEXT,
                    system_prompt_hash TEXT
                );
                CREATE TABLE system_prompts (
                    hash TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL
                );
                INSERT INTO sessions VALUES ('hashed', NULL, 'hash-a');
                INSERT INTO sessions VALUES ('inline', 'legacy prompt', NULL);
                INSERT INTO sessions VALUES ('empty', NULL, NULL);
                INSERT INTO system_prompts VALUES ('hash-a', 'stored prompt');
                INSERT INTO system_prompts VALUES ('orphan', 'orphan prompt');
                """
            )
            connection.commit()
            connection.close()

            cleared, mode = load_invalidation_module()._clear_stored_system_prompts_sqlite(database_path)

            self.assertEqual((cleared, mode), (2, "hash-backed"))
            connection = sqlite3.connect(database_path)
            self.assertEqual(
                connection.execute(
                    "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = 'hashed'"
                ).fetchone(),
                (None, None),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = 'inline'"
                ).fetchone(),
                (None, None),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM system_prompts").fetchone()[0],
                0,
            )
            connection.close()

            cleared, mode = load_invalidation_module()._clear_stored_system_prompts_sqlite(database_path)
            self.assertEqual((cleared, mode), (0, "hash-backed"))

    def test_hash_backed_schema_without_prompt_store_is_supported(self):
        module = load_plugin_module()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    system_prompt TEXT,
                    system_prompt_hash TEXT
                );
                INSERT INTO sessions VALUES ('hashed', NULL, 'hash-a');
                """
            )
            connection.commit()
            connection.close()

            cleared, mode = load_invalidation_module()._clear_stored_system_prompts_sqlite(database_path)

            self.assertEqual((cleared, mode), (1, "hash-backed"))
            connection = sqlite3.connect(database_path)
            self.assertEqual(
                connection.execute(
                    "SELECT system_prompt, system_prompt_hash FROM sessions"
                ).fetchone(),
                (None, None),
            )
            connection.close()

    def test_schema_without_any_prompt_columns_is_a_safe_no_op(self):
        module = load_plugin_module()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE sessions (id TEXT PRIMARY KEY);
                INSERT INTO sessions VALUES ('no-prompt-storage');
                """
            )
            connection.commit()
            connection.close()

            cleared, mode = load_invalidation_module()._clear_stored_system_prompts_sqlite(database_path)

            self.assertEqual((cleared, mode), (0, "none"))

    def test_legacy_inline_schema_retains_empty_string_invalidation(self):
        module = load_plugin_module()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    system_prompt TEXT
                );
                INSERT INTO sessions VALUES ('present', 'legacy prompt');
                INSERT INTO sessions VALUES ('empty', '');
                INSERT INTO sessions VALUES ('null', NULL);
                """
            )
            connection.commit()
            connection.close()

            cleared, mode = load_invalidation_module()._clear_stored_system_prompts_sqlite(database_path)

            self.assertEqual((cleared, mode), (1, "inline"))
            connection = sqlite3.connect(database_path)
            self.assertEqual(
                connection.execute(
                    "SELECT system_prompt FROM sessions WHERE id = 'present'"
                ).fetchone()[0],
                "",
            )
            self.assertEqual(
                connection.execute("SELECT system_prompt FROM sessions WHERE id = 'empty'").fetchone()[0],
                "",
            )
            self.assertIsNone(
                connection.execute("SELECT system_prompt FROM sessions WHERE id = 'null'").fetchone()[0]
            )
            connection.close()


if __name__ == "__main__":
    unittest.main()
