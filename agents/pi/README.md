# Pi refactor extension

Fix Java code smells one by one in a checked-out dataset case.

## Start

```bash
pi -e ./agents/pi/refactor.ts
```

Turn off other pi extensions if they get in the way.

## Commands

| Command | What it does |
|---|---|
| `/case <case-id>` | Check out the case, find smells, send the first one |
| `/next` | Apply the same verification gate as the automatic loop, then retry, skip, or advance |
| `/verify` | Run tests + smell check now |
| `/status` | Show current case progress |
| `/stop` | Stop the refactor loop |

Optional profile:

```text
/case Tap4j:4413ab35b400
/case Tap4j:4413ab35b400 --profile with-planning
```

## How a run goes

1. You run `/case …`.
2. The agent gets one smell and tries to fix it.
3. After each Java source change, including a deletion or move from bash, tests and smell detection run for you. Whole-worktree git restores are blocked.
4. The harness advances only when verification accepts the tests and confirms that the current smell is gone after the latest Java edit.
5. If the gate fails when the agent finishes, the harness sends the same smell again. Each dispatch is one attempt.
6. After five failed attempts, the harness records that smell as skipped and requests the next non-skipped smell.
7. When no non-skipped smells remain, the case ends and records the remaining smell count.

Transient LLM HTTP 429 rate limits wait (Retry-After or backoff) and resend the same attempt; they do not consume the five-attempt budget.

Attempts 1–2 use `deepseek/deepseek-v4-flash-0731`. After two real gate failures, attempts 3–5 use `upstage/solar-pro4`. Each new smell starts again with the weaker model.

Each attempt stops after 30 LLM turns if the model is still requesting tools. The normal verification gate then advances, retries, or skips the smell.

`agent_settled`, `/next`, and a changed smell count cannot bypass the gate. A new Java edit clears the previous acceptance until verification for that edit finishes. Unchanged, unrelated timing tests are rerun and can be accepted as flaky without ending the case. Disabling or removing tests is rejected. Fatal JDK or Maven environment failures still stop the case.

You usually only need `/case`. Use `/next` if the loop stalled. Use `/verify` or `/status` to inspect the current gate, attempt, and skipped smell keys. Use `/stop` to quit.

## Whole dataset (many cases)

Interactive `/case` is one case at a time. For the full manifest, use the batch runner — it starts several pi sessions in parallel, each in its own worktree:

```bash
# once
cd experiments && bun install && cd ..

# all cases in the manifest
node --experimental-strip-types experiments/pi_batch.ts dataset/manifest.jsonl

# safer first try
node --experimental-strip-types experiments/pi_batch.ts dataset/manifest.jsonl --limit 2 --concurrency 1

# bigger / other manifest
node --experimental-strip-types experiments/pi_batch.ts dataset/manifest_bigger.jsonl --concurrency 2
```

| Flag | Meaning |
|---|---|
| `--concurrency <n>` | How many cases run at once (default `2`) |
| `--limit <n>` | Only first N cases |
| `--case-id <id>` | Only this one case |
| `--profile <name>` | `without-planning` (default) or `with-planning` |
| `--timeout-ms <n>` | Give up on a stuck case (default 4 hours) |
| `--idle-timeout-ms <n>` | Abort if no tool/LLM/hook activity for N ms (default 4 hours, same as wall; `0` disables). SDK only has `httpIdleTimeoutMs` for silent LLM streams; the between-tool watchdog is ours. |
| `--llm-timeout-ms <n>` | Single LLM HTTP request timeout via `retry.provider.timeoutMs` (default 4 hours). |
| `--prepare-timeout-ms <n>` | Checkout + smell detect timeout (default 3 minutes). |
| `--quiet` | Do not stream assistant/tool text to stdout |

Each case gets its own checkout under `experiments/pi/worktrees/`. When a case finishes, metrics go to Eliot (`data/all.log`) and `data/pi/`.

LLM provider failures (HTTP 4xx/5xx, timeouts, credits, rate limits, retries) print to stderr as `[llm-error]` / `[llm-retry]`.

Agent text and tools stream to stdout via `session.subscribe` (SDK Events). Use `--quiet` to hide that. With `--concurrency > 1` lines from cases interleave (each line is prefixed with case id).

Batch must call `session.bindExtensions()` after `createAgentSession` — that is what fires `session_start` and starts the first smell. Without it the agent stays idle until the per-case timeout.
