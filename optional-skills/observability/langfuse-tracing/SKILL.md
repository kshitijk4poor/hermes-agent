---
name: langfuse-tracing
description: Add opt-in Langfuse tracing to Hermes by merging an external local feature repo. Tracing stays disabled until you explicitly enable it with Langfuse environment variables.
version: 0.1.0
author: Nous Research
license: MIT
metadata:
  hermes:
    tags: [observability, tracing, langfuse, telemetry, hooks]
    category: observability
---

# Langfuse Tracing for Hermes

This optional skill adds Langfuse tracing to Hermes without shipping the tracing code in Hermes core by default.

The installer skill lives inside Hermes, but the actual code changes come from a separate local feature repo that gets merged into your checkout when you run this skill.

This mirrors the same high-level opt-in model used by Langfuse's Claude Code integration:
- fail open if Langfuse is not configured
- opt in explicitly per machine/project
- keep tracing dormant until environment variables are set

## What this skill installs

The external feature repo adds:
- a small Hermes hook emission patch for LLM calls
- tool-call hook IDs so tool spans can be correlated reliably
- a project plugin at `.hermes/plugins/langfuse_tracing/`
- Langfuse Python dependency metadata

The plugin traces:
- LLM calls
- tool calls
- per-turn trace grouping keyed by the Hermes task/session

## Rules

1. Never merge this feature into a dirty working tree.
2. Never enable tracing without explicit user consent.
3. If Langfuse credentials are missing, leave tracing disabled after merge.
4. If merge conflicts appear in `uv.lock`, prefer regenerating it over guessing.

## Phase 1: Preflight

### Check for a clean working tree

Run:

```bash
git status --porcelain
```

If anything is dirty, stop and tell the user to commit or stash first.

### Resolve the local feature repo path

Use this resolution order:

1. `HERMES_LANGFUSE_FEATURE_REPO`
2. `https://github.com/kshitijk4poor/hermes-langfuse-plugin.git`

In shell:

```bash
FEATURE_REPO="${HERMES_LANGFUSE_FEATURE_REPO:-https://github.com/kshitijk4poor/hermes-langfuse-plugin.git}"
printf '%s\n' "$FEATURE_REPO"
```

If the override points to a local filesystem path, verify that path exists before continuing.

### Check whether the code is already present

If `.hermes/plugins/langfuse_tracing/plugin.yaml` already exists, skip to Phase 3.

## Phase 2: Apply the external feature repo

### Ensure the git remote exists

Run:

```bash
git remote -v
```

If `hermes-langfuse` is missing, add it:

```bash
git remote add hermes-langfuse "$FEATURE_REPO"
```

### Fetch and merge

```bash
git fetch hermes-langfuse main
git merge hermes-langfuse/main
```

If the merge conflicts:
- read the conflicted files
- keep the intent of both sides
- if `uv.lock` conflicts badly, finish the merge with the other files resolved and regenerate `uv.lock` afterward

### Install dependencies

Use the repo's virtualenv:

```bash
source .venv/bin/activate
python -m pip install -e .
```

If the project uses `uv` and `uv.lock` was regenerated, it is also fine to run:

```bash
uv lock
```

### Validate the merged code

Run:

```bash
source .venv/bin/activate
python -m py_compile run_agent.py model_tools.py .hermes/plugins/langfuse_tracing/__init__.py
python -m pytest tests/test_plugins.py -q
```

## Phase 3: Configure Langfuse

### Enable the project plugin loader

Hermes only loads project plugins when this is set:

```bash
HERMES_ENABLE_PROJECT_PLUGINS=true
```

### Enable tracing explicitly

Set one of these flags:

```bash
HERMES_LANGFUSE_ENABLED=true
```

or

```bash
TRACE_TO_LANGFUSE=true
```

### Required Langfuse credentials

Add these to `~/.hermes/.env`:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=http://localhost:3000
```

Optional:

```bash
HERMES_LANGFUSE_ENV=local
HERMES_LANGFUSE_RELEASE=local-skill
HERMES_LANGFUSE_SAMPLE_RATE=1.0
HERMES_LANGFUSE_MAX_CHARS=12000
```

### Local self-hosted Langfuse

If the user wants a local Langfuse instance and does not already have one:

1. Clone the official repo:

```bash
git clone https://github.com/langfuse/langfuse.git "$HOME/Projects/langfuse"
```

2. Start it:

```bash
cd "$HOME/Projects/langfuse"
docker compose up -d
```

3. Open `http://localhost:3000`
4. Create a Langfuse project
5. Copy the project API keys into `~/.hermes/.env`

## Phase 4: Restart Hermes

Restart Hermes after updating `~/.hermes/.env`, because plugin discovery and env loading happen at process startup.

## Phase 5: Verify

1. Start Hermes in this repo
2. Ask a simple question that triggers at least one tool call
3. Open Langfuse and confirm you see:
   - a Hermes turn trace
   - one or more LLM generation spans
   - tool spans nested under the same trace

## Troubleshooting

### The plugin does not load

Check:

```bash
grep HERMES_ENABLE_PROJECT_PLUGINS ~/.hermes/.env
test -f .hermes/plugins/langfuse_tracing/plugin.yaml && echo plugin-present
```

Then fully restart Hermes.

### Langfuse receives nothing

Check:

```bash
grep -E 'HERMES_LANGFUSE_ENABLED|TRACE_TO_LANGFUSE|LANGFUSE_' ~/.hermes/.env
```

Also verify the Langfuse server is reachable at `LANGFUSE_BASE_URL`.

### Dependency problems after merge

Re-run:

```bash
source .venv/bin/activate
python -m pip install -e .
```

If lockfile drift remains:

```bash
uv lock
```
