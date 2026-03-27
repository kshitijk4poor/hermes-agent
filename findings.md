# TTFT Findings

Date: 2026-03-27
Branch: `ttft-upstream-main`
Base: `upstream/main` @ `b8b1f24fd755ae187a0fbaedf5c9657a2af1ef1e`

## Goal

Figure out whether bad TTFT was model latency or Hermes doing too much before the first request, then keep the wins and record what actually helped.

## Follow-up Ideas Tested

- Move volatile runtime metadata out of the cached system prompt
- Add a disk-backed skills metadata snapshot for fresh processes
- Benchmark startup parallelization instead of assuming it helps

## Measurement Setup

- Clean worktree from `upstream/main`
- Python environment: `/Users/kshitij/Projects/hermes-agent/.venv`
- Local harnesses measured:
  - cold `AIAgent(...)` init
  - `_build_system_prompt()`
  - time from `run_conversation()` entry to the first outbound `chat.completions.create(...)` dispatch
- Follow-up harnesses additionally used a copied `HERMES_HOME` with the current `skills/`, `config.yaml`, and `SOUL.md`
  - copied skill inventory: `104` `SKILL.md` files
  - snapshot benchmark compared the same copied home with and without `.skills_prompt_snapshot.json`

## Baseline On Upstream Main

- Cold `AIAgent` init: `5499.24 ms`
- First outbound request dispatch: `1252.98 ms`
- `_build_system_prompt()`: `1149.83 ms`

Baseline hotspots:

- `vision_analyze` requirements check: `3963.62 ms`
- `_build_system_prompt()` sub-costs:
  - `check_toolset_requirements`: `442.13 ms`
  - `build_skills_system_prompt`: `659.61 ms`

Conclusion: the worst latency was not just provider TTFT. Hermes was burning a lot of time before the request ever left the process.

## Findings

### 1. Tool availability checks were duplicated

What we tried:

- Cache shared `check_fn` results inside one `ToolRegistry.get_definitions()` call.

What worked:

- Repeated tools that shared the same availability probe stopped rerunning the same check during schema build.
- This was one of the fixes that drove cold init from `5499 ms` down to `963 ms` in the first round.

Files:

- `tools/registry.py`
- `tests/tools/test_registry.py`

### 2. Vision auto-resolution was probing too many backends

What we tried:

- Stop calling the full “discover every vision backend” path for auto resolution.
- Prefer the main configured provider first and short-circuit on the first working backend.

What worked:

- The `vision_analyze` gating check dropped from `3963.62 ms` to `76.61 ms`.
- This removed the single biggest cold-start offender.

Files:

- `agent/auxiliary_client.py`
- `tests/agent/test_auxiliary_client.py`

### 3. Anthropic adapter had eager import-time work

What we tried:

- Remove eager Claude Code version detection at module import time.
- Resolve and cache it lazily only when OAuth headers actually need it.

What worked:

- The import-time tax disappeared from the Anthropic path that vision fallback and other startup flows touched.

Files:

- `agent/anthropic_adapter.py`
- `tests/test_anthropic_adapter.py`

### 4. `_build_system_prompt()` was recomputing toolset availability

What we tried:

- Stop calling `check_toolset_requirements()` again during system prompt construction.
- Derive available toolsets directly from already loaded `self.valid_tool_names`.

What worked:

- Removed the extra `442.13 ms` prompt-build pass from the hot path.
- After the first four fixes, `_build_system_prompt()` dropped from `1149.83 ms` to `438.82 ms`.
- In the same first-round harness, first outbound request dispatch dropped from `1252.98 ms` to `434.56 ms`.

Files:

- `run_agent.py`
- `tests/test_run_agent.py`

### 5. Skills prompt generation was still a repeat cost

What we tried first:

- A signature-validated cache for `build_skills_system_prompt()`.

What happened:

- It improved reuse, but validating the cache was still expensive enough that it hurt the cold path.
- I did not keep that version.

What we kept:

- A lightweight in-process cache for the skills system prompt.
- Explicit invalidation when `skill_manage(...)` modifies skills.
- Prompt-builder-local parsing so this path does not need to import the heavy `tools` package just to read skill frontmatter.

What worked:

- In one Python process:
  - `_build_system_prompt()` first fresh agent: `545.95 ms`
  - `_build_system_prompt()` second fresh agent: `0.36 ms`
- Standalone `build_skills_system_prompt()`:
  - first call: `762.02 ms`
  - second call: `0.19 ms`

Interpretation:

- This is a hot-path win for repeated new agents in the same Hermes process.
- The remaining cold-path bottleneck is still skill frontmatter parsing across installed skills.

Files:

- `agent/prompt_builder.py`
- `tools/skill_manager_tool.py`
- `tests/agent/test_prompt_builder.py`
- `tests/tools/test_skill_manager_tool.py`

### 6. Remaining cold-path bottleneck

Current dominant remaining pre-request work:

- `build_skills_system_prompt()` still spends most of its time parsing `SKILL.md` frontmatter.
- The integrated profile also showed non-trivial timestamp/timezone work via `hermes_time.now()` on the first prompt build.

What this means:

- The big avoidable structural regressions are fixed.
- The next big step, if we want more cold-start improvement, is probably a maintained skill metadata index or startup snapshot instead of reparsing every skill file on the first new agent in a process.

### 7. Runtime metadata belonged outside the cached system prompt

What we tried:

- Removed live runtime lines from `_build_system_prompt()`
- Added a per-turn runtime note to the current user message instead:
  - `Current date/time`
  - `Session ID` when enabled
  - `Model`
  - `Provider`

What worked:

- The cached system prompt is now stable across new sessions instead of varying on timestamp/session metadata.
- This aligns Hermes with the Claude Code style prompt-caching pattern from the comparison pass.
- The local timing effect was smaller than the skills work, but the real value is improved provider-side prefix-cache reuse.

Files:

- `run_agent.py`
- `tests/test_run_agent.py`

### 8. Disk-backed skills metadata snapshot fixed the remaining fresh-process cost

What we tried:

- Kept the in-process skills prompt cache
- Added `.skills_prompt_snapshot.json` under `HERMES_HOME`
- Snapshot validity is checked from a manifest of `SKILL.md` and `DESCRIPTION.md` mtimes/sizes
- `skill_manage(...)` now clears both the in-process cache and the disk snapshot after successful writes

What worked:

- On the copied `104`-skill benchmark home:
  - fresh-process `_build_system_prompt()` without snapshot: `297.22 ms`
  - fresh-process `_build_system_prompt()` with snapshot: `102.51 ms`
  - fresh-process first outbound dispatch without snapshot: `368.42 ms`
  - fresh-process first outbound dispatch with snapshot: `115.60 ms`
- Same-process fresh-agent prompt build remained effectively hot:
  - first agent: `85.34 ms`
  - second agent: `0.88 ms`

Interpretation:

- This is the follow-up idea that materially moved fresh-process TTFT.
- Relative to the original upstream baseline:
  - `_build_system_prompt()` improved from `1149.83 ms` to `102.51 ms`
  - first outbound dispatch improved from `1252.98 ms` to `115.60 ms`

Files:

- `agent/prompt_builder.py`
- `tools/skill_manager_tool.py`
- `tests/agent/test_prompt_builder.py`
- `tests/tools/test_skill_manager_tool.py`

### 9. Parallelizing the remaining startup work was not worth keeping

What we tried:

- Benchmarked the remaining post-snapshot prompt-assembly work both sequentially and with a small `ThreadPoolExecutor`
- Compared:
  - `load_soul_md()`
  - `build_skills_system_prompt()`
  - `build_context_files_prompt(...)`

What happened:

- On the copied benchmark home:
  - sequential median: `99.80 ms`
  - parallel median: `94.64 ms`
- The gain was tiny and noisy, and the parallel version had a worse outlier (`156.82 ms`)

Decision:

- I did not keep a production parallel-startup patch.
- After the snapshot work, there is not enough expensive independent work left to justify the complexity and variance.

## Kept Results

Measured wins from the clean-worktree investigation:

- Cold `AIAgent` init: `5499 ms -> 963 ms`
- First outbound request dispatch after the first four fixes: `1253 ms -> 435 ms`
- `_build_system_prompt()` after the first four fixes: `1150 ms -> 439 ms`
- Repeated same-process prompt builds after the skills cache: `545.95 ms -> 0.36 ms`
- Fresh-process `_build_system_prompt()` on the copied skills home after the disk snapshot: `297.22 ms -> 102.51 ms`
- Fresh-process first outbound dispatch on the copied skills home after the disk snapshot: `368.42 ms -> 115.60 ms`

## Not Kept

- Startup parallelization was tested directly and not kept.
- Measured result on the copied benchmark home:
  - sequential median: `99.80 ms`
  - parallel median: `94.64 ms`
- The delta was too small and noisy to justify thread-pool complexity, and the parallel version had a worse outlier (`156.82 ms`).

## Comparative Patterns From Other Agent CLIs

Patterns worth stealing or keeping in mind:

- Codex: parallel startup tasks, startup tool snapshots, long-lived session client, connection prewarm
- Claude Code: move date out of the cached system prompt, defer non-critical loading, cache MCP/auth discovery failures
- Gemini CLI: dynamically import heavy interactive UI, emphasize context efficiency, cache expensive hook work
- OpenCode: bootstrap parallel fetches, render partial state early, keep background loading separate from first paint
- Aider: defer slow imports into background work, keep stable prompt regions cache-friendly

Hermes follow-up ideas from that comparison:

- Move the live date/time out of the cached system prompt to improve provider-side prompt cache reuse across new sessions
- Maintain a skill metadata snapshot instead of reparsing every `SKILL.md` on the first agent in a process
- Consider parallelizing non-critical startup work where correctness does not depend on sequencing

## Sources

- OpenAI latency optimization guide: https://developers.openai.com/api/docs/guides/latency-optimization
- Anthropic prompt caching docs: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- DeepWiki, `NousResearch/hermes-agent`: https://deepwiki.com/search/which-parts-of-aiagent-initial_10c46a12-abce-4d26-9e39-3d3668c3a1c5
- DeepWiki, `openai/codex`: https://deepwiki.com/search/what-patterns-does-this-cli-us_d4c4a710-3be6-410e-a8a2-3191861df4d0
- DeepWiki, `anthropics/claude-code`: https://deepwiki.com/search/what-patterns-does-this-cli-us_2e039217-d1fe-4a0f-9a2a-30446700cee7
- DeepWiki, `google-gemini/gemini-cli`: https://deepwiki.com/search/what-patterns-does-this-cli-us_1272507f-3eda-4e81-bfdc-5166a36c76f4
- DeepWiki, `sst/opencode`: https://deepwiki.com/search/what-patterns-does-this-cli-us_28bbda01-2d01-46f2-a683-4cdf60534862
- DeepWiki, `Aider-AI/aider`: https://deepwiki.com/search/what-patterns-does-this-cli-us_485a9564-7cdd-4007-a0ca-4386a8eb5fa9
