# hermes-evolve

Hot-reload Hermes plugins and evolve the system prompt in-place — no restart,
no lost session history. `/evolve now` reloads all loaded plugin modules,
re-registers hooks / commands / tools, clears caches, and triggers a fresh system
prompt rebuild on the next message.

Works across **CLI, TUI, Desktop, and Gateway** — all modes share the same
`state.db`, and clearing all stored system prompts forces every active session
to rebuild its prompt on the next turn.

> Design: follows `AGENTS.md` § "Prompt Caching Must Not Break" — explicitly
> opt-in, never automatic. The ONLY time Hermes alters context is during context
> compression; this command follows the canonical `--now` opt-in pattern.

## Install

```bash
pip install hermes-evolve
```

Then add to `config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-evolve
```

Restart Hermes.

## Usage

```
/evolve now      — reload plugins, re-register, clear caches, clear prompts
/evolve status   — show loaded plugin modules and what will be affected
```

### What `/evolve now` does

| Step | Action |
|:----:|--------|
| 1 | `importlib.reload` all loaded plugin modules (user plugins + bundled) |
| 2 | `discover_plugins(force=True)` — re-scan directories, re-run `register(ctx)` for every plugin |
| 3 | Clear `model_tools._tool_defs_cache` + bump `registry._generation` — forces tool schema rebuild |
| 4 | `UPDATE sessions SET system_prompt = ''` on `state.db` — all active sessions rebuild on next message |

Symlinks are resolved correctly — plugins installed via `ln -sf` are discovered
by their real path.

### After running

Your next message in **any** active session triggers a full system prompt
rebuild: new SOUL.md, new MEMORY.md, new tool schemas, new hook handlers.
One cache miss. Session history intact.

## Config

No configuration required. The plugin registers only the `/evolve` slash command.

## How it works

The system prompt is frozen per-session by Hermes for [prompt-caching
reasons](https://hermes-agent.nousresearch.com/docs).  `_restore_or_build_system_prompt()`
stores the prompt in `state.db` and reuses it verbatim every turn.  `/evolve now`
clears those stored prompts so the next turn rebuilds from the current files.

## Development

```bash
git clone https://github.com/nuffin/hermes-evolve
cd hermes-evolve
pip install -e .
```

No test suite — verification is manual against a running Hermes instance.

## License

MIT
