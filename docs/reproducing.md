# Reproducing the experiments

This document describes how to rerun the pi harness experiments: four smell
order planners on six Sousa composite-refactoring cases, with ORGANIC
detection, Gradle tests, and CK metrics after each case.

Entry point: `experiments/pi_batch.ts` drives `agents/pi/refactor.ts`, which
calls Python hooks in `agents/pi/hooks.py`.

This path does **not** use the LangGraph runner (`experiments/runner.py`).

## What the experiment does

For each dataset case the harness:

1. Checks out a pre-refactoring Java commit into a worktree.
2. Detects smells with ORGANIC and orders them with a planner.
3. Sends one smell at a time to the coding agent.
4. After each Java edit, runs Gradle tests and ORGANIC again.
5. Advances only when tests pass and the current smell is gone.
6. On case finish, records CK metrics (CBO, LCOM, WMC, LOC).

Four planners on six cases = 24 runs:

| CLI `--planner` | Name |
|-----------------|------|
| `none` | Unsorted (detector order) |
| `greedy` | DAPS |
| `topo` | TopS |
| `bfs` | BeFS |

## Prerequisites

- Python 3.13 (`mise install` or `uv`)
- Node.js or Bun (for `pi_batch.ts`)
- Git
- SDKMAN with Java 8 and 17 (see `.sdkmanrc`)
- OpenRouter API key

## Setup

```bash
# Python deps
uv sync

# pi SDK deps
cd experiments && bun install && cd ..

# API key (create locally; not tracked in git)
cat >> mise.local.toml <<'EOF'
[env]
OPENROUTER_API_KEY = "your-key-here"
EOF

# ORGANIC smell detector (gitignored vendored clone)
git clone https://github.com/opus-research/organic-standalone \
  detection/organic-standalone

# CK metrics tool (gitignored vendored clone)
git clone https://github.com/mauricioaniche/ck java/metrics/ck
```

Build the Spoon AST indexer once (used by DAPS, TopS, and BeFS):

```bash
cd planning/ast && ./gradlew shadowJar && cd ../..
```

## Dataset manifest

Six cases from the Sousa composite-refactoring dataset are listed in
`dataset/manifest_eval6.jsonl`.

Case IDs:

- `Drugis Common:bcbdecd601d0`
- `Drugis Common:6cd5f088448e`
- `IRC Bot (c2nes/ircbot):358b21bb264e`
- `IRC Bot (c2nes/ircbot):091c08525c3b`
- `PhiCode Philib:4e4f6e9da6ea`
- `Tap4j:462ab4c8c308`

The harness clones each project's GitHub repo on first use. Cloned repos
land in `experiments/pi/worktrees/` (gitignored). You do not need
`dataset/projects/`.

To regenerate manifests from Neo4j instead of using the pre-built file,
see `dataset/generation/generate_manifest.py` and `dataset/datasets.config.toml`.
That path needs a running Neo4j instance and is not required to rerun the
eval set.

## Run all 24 experiments

```bash
for planner in none greedy topo bfs; do
  node --experimental-strip-types experiments/pi_batch.ts \
    dataset/manifest_eval6.jsonl \
    --planner "$planner" \
    --concurrency 2
done
```

Useful flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `--concurrency <n>` | 2 | Cases in parallel |
| `--limit <n>` | all | First N manifest rows |
| `--case-id <id>` | all | Single case only |
| `--profile <name>` | `without-planning` | YAML prompt variant |
| `--timeout-ms <n>` | 4h | Wall clock per case |
| `--idle-timeout-ms <n>` | 4h | Abort if no activity |
| `--quiet` | off | Hide agent stream |

Models (set in `agents/pi/refactor.ts`):

- Attempts 1–2: `deepseek/deepseek-v4-flash-0731`
- Attempts 3–5: `upstage/solar-pro4`

Interactive single-case mode:

```bash
pi -e ./agents/pi/refactor.ts
# then: /case Tap4j:462ab4c8c308
```

## Outputs

Per-case logs and metrics go under `data/pi/` (gitignored). Each case gets
an Eliot log and a run directory. Batch runs also write
`data/pi/runs/batch_<uuid>/all.log`.

Do not commit `data/`, worktrees, or logs.

## Post-run analysis

Write results under `docs/figures/` (create the directory if needed).

Dependency-rule agreement:

```bash
mkdir -p docs/figures
uv run python -m agents.pi.dependency_effects \
  data/pi/sweep_logs/*.log \
  --json docs/figures/dependency_effects.json \
  --markdown docs/figures/dependency_effects.md
```

Aggregate smell and cost stats:

```bash
uv run python -m agents.pi.smell_stats batch --batch-prefix <batch-uuid-prefix>
```

## Code layout (what matters)

```
agents/pi/           pi extension + Python hooks
agents/deep/         verification, profiles, smell advice markdown
agents/java_test/    test failure analysis
planning/            four planners + Markovič dependency rules
detection/           ORGANIC wrapper (clone organic-standalone separately)
java/                Gradle test runner, CK wrapper
testing/             Surefire parsing, flaky-test handling
dataset/             manifest schema + generation scripts
repository/          git checkout and worktree helpers
smell/               smell model
experiments/         pi_batch.ts batch runner
docs/                this guide and optional analysis outputs
```

## What is not part of this reproduction

- `experiments/runner.py` — LangGraph composite workflow (older path)
- `workflows/composite/graph.py` — same
- `agents/pydantic_deep/` — separate agent stack
- `data/` — runtime logs and outputs
- `dataset/projects/` — cached clones from manifest generation

## Tests

```bash
uv run pytest tests/test_pi_hooks.py tests/test_smell_run.py \
  tests/test_befs_planner.py tests/test_topo_adversarial.py \
  tests/test_order_planners.py tests/test_dependency_effects.py
```

TypeScript harness tests:

```bash
node --experimental-strip-types --test experiments/pi_batch.test.ts \
  experiments/refactor_bash_timeout.test.ts \
  experiments/refactor_llm_rate_limit.test.ts
```
