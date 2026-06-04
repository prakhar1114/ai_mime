# Developer Guide

This guide is for developers evaluating or contributing to ai_mime.

## Setup
```bash
git clone --recurse-submodules https://github.com/prakhar1114/ai_mime
cd ai_mime

uv venv .venv
source .venv/bin/activate
uv pip install -e .
uv tool install --python .venv/bin/python --with-editable packages/llm-resolver harness/browser-harness
```

Start the app:

```bash
start_app
```

Run tests:

```bash
uv run pytest tests -q
```

## Repository Layout
| Path | Purpose |
| --- | --- |
| `src/ai_mime/record/` | Recording process, screenshot capture, audio/input capture helpers. |
| `src/ai_mime/reflect/` | Recording-to-schema compiler and optimized-plan generation. |
| `src/ai_mime/agent_runner/` | Build, replay, and workspace agent orchestration. |
| `src/ai_mime/editor/` | FastAPI dashboard and static web UI. |
| `harness/browser-harness/` | Browser automation harness used by generated skills. |
| `packages/llm-resolver/` | Shared LLM configuration and structured model calls. |
| `docs/architecture.md` | Pipeline-level architecture. |

## Key Commands
```bash
# Start the menu bar app and dashboard.
start_app

# Reflect the latest recording under recordings/.
reflect

# Reflect a specific recording.
reflect --session <recording_id>

# Run an already-built skill manually.
cd workflows/<workflow_id>/skills/<skill_slug>
./run.sh inputs/inputs.example.json
```

## Runtime Artifacts
| Path | Meaning |
| --- | --- |
| `recordings/<id>/manifest.jsonl` | Raw event stream captured during recording. |
| `workflows/<id>/schema.json` | Semantic workflow generated from the recording. |
| `workflows/<id>/optimized_plan.json` | Executor strategy used by the skill builder. |
| `workflows/<id>/agent/` | Build-session state, notes, confirmed inputs, and status. |
| `workflows/<id>/skills/<slug>/` | Portable skill package. |
| `workflows/<id>/runs/` | Replay logs, outputs, and copied assets. |

## Provider Configuration
The onboarding wizard supports:

- Anthropic / Claude Code
- OpenAI / Codex

The normal path writes `.env` and `user_config.yml` for you. For custom models
or providers, edit `user_config.yml` and set `provider: custom` with explicit
LLM and agent sections. `packages/llm-resolver/src/llm_resolver/config.py`
contains the config schema and built-in defaults.

## Generated Skill Contract
A built skill lives at `workflows/<id>/skills/<slug>/` and includes:

```text
SKILL.md
run.sh
scripts/run.py
inputs/inputs.example.json
inputs/inputs.template.json
references/fallback_plan.md
```

`run.sh` accepts an optional path to an inputs JSON file. `scripts/run.py`
should read inputs up front, emit JSON progress events on stderr, and exit
non-zero on failure.

Common progress events:

```json
{"event":"step_start","id":"<step_id>","title":"..."}
{"event":"step_done","id":"<step_id>","outputs":{},"summary":"..."}
{"event":"step_failed","id":"<step_id>","error":"...","recoverable":true}
{"event":"workflow_done","outputs":{}}
```

## Runtime Environment
ai_mime exports these variables to generated skills:

- `AI_MIME_PYTHON_PATH`: Python interpreter for workflow scripts.
- `AI_MIME_UV_PATH`: `uv` binary for build-time dependency setup or repair.
- `AI_MIME_BROWSER_HARNESS_BIN`: browser-harness executable.
- `AI_MIME_BROWSER_SKILL_PATH`: bundled browser-harness skill/resources.
- `AI_MIME_UI_AGENT_CMD`: command for native macOS UI-agent fallback.
- `AI_MIME_CONFIG_PATH`: app-owned provider config.

Generated skill code should use these variables instead of hardcoded local
checkout paths.
