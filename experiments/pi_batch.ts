/**
 * Non-interactive pi deep-refactor batch runner.
 *
 * Prepares each manifest case into its own worktree, runs a dedicated
 * AgentSession with only the refactor extension, and waits for
 * `deep-refactor:complete` (whole-case finish — not per-edit verification).
 * Completion logs Eliot case summary + CK metrics via the Python complete hook.
 *
 * Usage:
 *   node --experimental-strip-types experiments/pi_batch.ts dataset/manifest.jsonl
 *   node --experimental-strip-types experiments/pi_batch.ts dataset/manifest.jsonl --concurrency 2 --limit 3
 *   bun experiments/pi_batch.ts dataset/manifest.jsonl
 */

import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import {
	createAgentSession,
	createEventBus,
	DefaultResourceLoader,
	getAgentDir,
	ModelRuntime,
	SessionManager,
	SettingsManager,
	type AgentSession,
	type AgentSessionEvent,
} from "@earendil-works/pi-coding-agent";
import {
	ACTIVITY_EVENT,
	COMPLETE_EVENT,
	STOP_EVENT,
	STRONG_MODEL,
	WEAK_MODEL,
	createDeepRefactorExtension,
	isBatchFatalLlmStop,
	logPiEliot,
	type DeepRefactorCompleteEvent,
	type PreparePayload,
} from "../agents/pi/refactor.ts";
import { applyMiseTomlEnv } from "../agents/pi/mise_env.ts";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(SCRIPT_DIR, "..");
const DEFAULT_PROFILE = "without-planning";
const DEFAULT_CONCURRENCY = 2;
const PLANNER_CHOICES = ["none", "bfs", "greedy", "topo"] as const;
type PlannerChoice = (typeof PLANNER_CHOICES)[number];
const THINKING_LEVEL = "high";
/** Hard wall clock per case (includes agent thrashing). */
const DEFAULT_TIMEOUT_MS = 4 * 60 * 60 * 1000;
/** Abort a case if no tool/LLM/hook activity for this long (0 disables). */
const DEFAULT_IDLE_TIMEOUT_MS = DEFAULT_TIMEOUT_MS;
/** Per-LLM-request HTTP timeout (provider streamSimple timeoutMs). */
const DEFAULT_LLM_TIMEOUT_MS = DEFAULT_TIMEOUT_MS;
/** Checkout + smell detect before the agent session starts. */
const DEFAULT_PREPARE_TIMEOUT_MS = 3 * 60 * 1000;
/** Let an in-flight extension completion event arrive after the wall deadline. */
const WALL_TIMEOUT_GRACE_MS = 30_000;
/** Never leave the worker pool blocked by a slow final Python hook. */
const COMPLETE_TIMEOUT_MS = 5 * 60 * 1000;

export function activityHeartbeatInterval(idleTimeoutMs: number): number {
	/** Return a hook heartbeat interval that precedes the idle deadline. */
	return idleTimeoutMs > 0 ? Math.max(1, Math.floor(idleTimeoutMs / 2)) : 10_000;
}

export function remainingTimeoutDelay(
	startedAtMs: number,
	timeoutMs: number,
	currentTimeMs: number,
): number {
	/** Return the time left without trusting a timer callback to be punctual. */
	const elapsedMs = Math.max(0, currentTimeMs - startedAtMs);
	return Math.max(0, Math.ceil(timeoutMs - elapsedMs));
}

export function isExperimentCompleted(
	stopReason: string,
	remainingSmells: number,
	introducedSmells: number,
): boolean {
	return (
		(stopReason === "smells_cleared" || stopReason === "smells_exhausted") &&
		remainingSmells === 0 &&
		introducedSmells === 0
	);
}

/** Case succeeded only when smells are gone and the complete hook collected CK. */
export function isCaseOk(
	stopReason: string,
	remainingSmells: number,
	introducedSmells: number,
	ckError: string | null,
): boolean {
	return (
		ckError === null &&
		isExperimentCompleted(stopReason, remainingSmells, introducedSmells)
	);
}

export function withUsageJsonArg(
	args: string[],
	usageJson: string | null | undefined,
): string[] {
	if (!usageJson) {
		return args;
	}
	return [...args, "--usage-json", usageJson];
}

function logLlmError(caseId: string, kind: string, detail: string): void {
	console.error(`[llm-error] ${caseId} ${kind}: ${detail}`);
}

function formatJson(value: unknown): string {
	try {
		return JSON.stringify(value, null, 2);
	} catch {
		return String(value);
	}
}

/**
 * Official SDK pattern: session.subscribe for streaming + tool lifecycle.
 * See https://pi.dev/docs/latest/sdk#events
 */
function attachSessionLogging(
	session: AgentSession,
	caseId: string,
	options: { showActivity: boolean },
): () => void {
	const showActivity = options.showActivity;
	let assistantOpen = false;
	return session.subscribe((event: AgentSessionEvent) => {
		switch (event.type) {
			case "message_start": {
				if (!showActivity) {
					return;
				}
				const message = event.message;
				if (message && typeof message === "object" && "role" in message && message.role === "assistant") {
					process.stdout.write(`\n[${caseId}] assistant: `);
					assistantOpen = true;
				} else if (message && typeof message === "object" && "role" in message && message.role === "user") {
					const content =
						typeof message.content === "string"
							? message.content
							: formatJson(message.content);
					console.log(`\n[${caseId}] user:\n${content}`);
				}
				return;
			}
			case "message_update": {
				if (!showActivity) {
					return;
				}
				const update = event.assistantMessageEvent;
				if (update.type === "text_delta") {
					process.stdout.write(update.delta);
				} else if (update.type === "thinking_delta") {
					process.stdout.write(update.delta);
				}
				return;
			}
			case "message_end": {
				if (showActivity && assistantOpen) {
					process.stdout.write("\n");
					assistantOpen = false;
				}
				const message = event.message;
				if (!message || typeof message !== "object" || !("role" in message)) {
					return;
				}
				if (message.role !== "assistant") {
					return;
				}
				if (!("stopReason" in message) || message.stopReason !== "error") {
					return;
				}
				const detail =
					typeof message.errorMessage === "string" && message.errorMessage
						? message.errorMessage
						: "(assistant stopReason=error, no errorMessage)";
				logLlmError(caseId, "assistant", detail);
				return;
			}
			case "tool_execution_start": {
				if (!showActivity) {
					return;
				}
				console.log(`\n[${caseId}] tool ${event.toolName}\n${formatJson(event.args)}`);
				return;
			}
			case "tool_execution_end": {
				if (!showActivity) {
					return;
				}
				console.log(
					`[${caseId}] tool ${event.toolName} ${event.isError ? "ERROR" : "ok"}`,
				);
				if ("result" in event) {
					console.log(formatJson(event.result));
				}
				return;
			}
			case "auto_retry_start":
				console.error(
					`[llm-retry] ${caseId} attempt ${event.attempt}/${event.maxAttempts} in ${event.delayMs}ms: ${event.errorMessage}`,
				);
				return;
			case "auto_retry_end":
				if (event.success) {
					console.error(`[llm-retry] ${caseId} recovered after attempt ${event.attempt}`);
				} else {
					logLlmError(
						caseId,
						"retries_exhausted",
						event.finalError ?? `failed after attempt ${event.attempt}`,
					);
				}
				return;
			case "compaction_end":
				if (event.errorMessage) {
					logLlmError(caseId, "compaction", event.errorMessage);
				}
				return;
			default:
				return;
		}
	});
}

type ManifestCase = {
	case_id: string;
	project: string;
};

type CompletePayload = {
	case_id: string;
	stop_reason: string;
	remaining_smells: number;
	fixed_smells: number;
	introduced_smells: number;
	original_smells: number;
	total_introduced_smells: number;
	fixed_introduced_smells: number;
	ck_error: string | null;
};

type CaseResult = {
	caseId: string;
	project: string;
	ok: boolean;
	stopReason: string;
	fixedSmellCount: number;
	introducedSmellCount: number;
	fixedIntroducedSmellCount: number;
	totalIntroducedSmellCount: number;
	originalSmellCount: number;
	remainingSmellCount: number;
	ckError: string | null;
	error: string | null;
	repoPath: string;
};

export type ManifestResumeCaseStatus = "successful" | "failed" | "skipped" | "pending";

export type ManifestResumeCase = {
	index: number;
	case_id: string;
	project: string;
	status: ManifestResumeCaseStatus;
	stop_reason: string | null;
	ok: boolean | null;
	original_smells: number | null;
	fixed_smells: number | null;
	remaining_smells: number | null;
	total_introduced_smells: number | null;
	fixed_introduced_smells: number | null;
	introduced_smells: number | null;
	error: string | null;
};

export type ManifestResume = {
	completed_count: number;
	pending_count: number;
	successful_count: number;
	failed_count: number;
	skipped_count: number;
	cases: ManifestResumeCase[];
};

function manifestResumeStatus(result: CaseResult): ManifestResumeCaseStatus {
	if (result.stopReason === "batch_aborted") {
		return "skipped";
	}
	return result.ok ? "successful" : "failed";
}

function manifestResumeCase(
	index: number,
	manifestCase: ManifestCase,
	result: CaseResult | undefined,
): ManifestResumeCase {
	if (!result) {
		return {
			index,
			case_id: manifestCase.case_id,
			project: manifestCase.project,
			status: "pending",
			stop_reason: null,
			ok: null,
			original_smells: null,
			fixed_smells: null,
			remaining_smells: null,
			total_introduced_smells: null,
			fixed_introduced_smells: null,
			introduced_smells: null,
			error: null,
		};
	}
	return {
		index,
		case_id: result.caseId,
		project: result.project,
		status: manifestResumeStatus(result),
		stop_reason: result.stopReason,
		ok: result.ok,
		original_smells: result.originalSmellCount,
		fixed_smells: result.fixedSmellCount,
		remaining_smells: result.remainingSmellCount,
		total_introduced_smells: result.totalIntroducedSmellCount,
		fixed_introduced_smells: result.fixedIntroducedSmellCount,
		introduced_smells: result.introducedSmellCount,
		error: result.error,
	};
}

/** Summarize manifest case outcomes for structured Eliot logging. */
export function buildManifestResume(
	manifestCases: readonly ManifestCase[],
	results: readonly CaseResult[],
): ManifestResume {
	const resultsByCaseId = new Map(results.map((result) => [result.caseId, result]));
	const cases = manifestCases.map((manifestCase, index) =>
		manifestResumeCase(index + 1, manifestCase, resultsByCaseId.get(manifestCase.case_id)),
	);
	const completed = cases.filter((entry) => entry.status !== "pending");
	return {
		completed_count: completed.length,
		pending_count: cases.length - completed.length,
		successful_count: completed.filter((entry) => entry.status === "successful").length,
		failed_count: completed.filter((entry) => entry.status === "failed").length,
		skipped_count: completed.filter((entry) => entry.status === "skipped").length,
		cases,
	};
}

function projectRelativeRunLog(runLogPath: string): string {
	const absolute = isAbsolute(runLogPath) ? runLogPath : resolve(REPO_ROOT, runLogPath);
	return relative(REPO_ROOT, absolute);
}

async function createBatchRunLog(manifestPath: string): Promise<string> {
	const runDir = resolve(REPO_ROOT, "data/pi/runs", `batch_${randomUUID()}`);
	await mkdir(runDir, { recursive: true });
	const runLog = resolve(runDir, "all.log");
	await writeFile(runLog, "", { flag: "a" });
	return projectRelativeRunLog(runLog);
}

function logManifestResume(
	runLog: string,
	options: {
		manifestPath: string;
		planner: string | null;
		manifestCases: readonly ManifestCase[];
		results: readonly CaseResult[];
		trigger: "case_end" | "batch_end";
		trigger_case_id: string | null;
	},
): void {
	const resume = buildManifestResume(options.manifestCases, options.results);
	logPiEliot(runLog, {
		message_type: "pi_batch:manifest_resume",
		trigger: options.trigger,
		trigger_case_id: options.trigger_case_id,
		manifest: options.manifestPath,
		planner: options.planner ?? process.env.PLANNING_PLANNER ?? null,
		...resume,
	});
}

function formatSmellProgress(metrics: {
	fixedSmellCount: number;
	originalSmellCount: number;
	fixedIntroducedSmellCount: number;
	totalIntroducedSmellCount: number;
	introducedSmellCount: number;
}): string {
	const original = `${metrics.fixedSmellCount}/${metrics.originalSmellCount} original`;
	const introduced =
		metrics.totalIntroducedSmellCount > 0
			? `${metrics.fixedIntroducedSmellCount}/${metrics.totalIntroducedSmellCount} introduced`
			: `${metrics.introducedSmellCount} introduced remaining`;
	return `${original}, ${introduced}`;
}

type SelectedModel = NonNullable<ReturnType<ModelRuntime["getModel"]>>;

function usage(): never {
	console.error(`Usage: node --experimental-strip-types experiments/pi_batch.ts <manifest.jsonl> [options]

Options:
  --concurrency <n>   Parallel pi sessions (default: ${DEFAULT_CONCURRENCY})
  --limit <n>         Run at most N cases
  --profile <name>    Deep profile (default: ${DEFAULT_PROFILE})
  --planner <name>    PLANNING_PLANNER override (${PLANNER_CHOICES.join("|")}; default: config.toml)
  --timeout-ms <n>    Per-case wall timeout (default: ${DEFAULT_TIMEOUT_MS})
  --idle-timeout-ms <n>  Abort if no tool/LLM activity for N ms (default: ${DEFAULT_IDLE_TIMEOUT_MS}; 0=off)
  --llm-timeout-ms <n>   Single LLM HTTP request timeout (default: ${DEFAULT_LLM_TIMEOUT_MS})
  --prepare-timeout-ms <n>  prepare hook timeout (default: ${DEFAULT_PREPARE_TIMEOUT_MS})
  --case-id <id>      Run only this case_id
  --quiet             Do not stream assistant/tool activity to stdout
`);
	process.exit(2);
}

function isPlannerChoice(value: string): value is PlannerChoice {
	return (PLANNER_CHOICES as readonly string[]).includes(value);
}

function parseArgs(argv: string[]): {
	manifest: string;
	concurrency: number;
	limit: number | null;
	profile: string;
	planner: PlannerChoice | null;
	timeoutMs: number;
	idleTimeoutMs: number;
	llmTimeoutMs: number;
	prepareTimeoutMs: number;
	caseId: string | null;
	quiet: boolean;
} {
	if (argv.length === 0 || argv[0] === "-h" || argv[0] === "--help") {
		usage();
	}
	const manifest = argv[0];
	let concurrency = DEFAULT_CONCURRENCY;
	let limit: number | null = null;
	let profile = DEFAULT_PROFILE;
	let planner: PlannerChoice | null = null;
	let timeoutMs = DEFAULT_TIMEOUT_MS;
	let idleTimeoutMs = DEFAULT_IDLE_TIMEOUT_MS;
	let llmTimeoutMs = DEFAULT_LLM_TIMEOUT_MS;
	let prepareTimeoutMs = DEFAULT_PREPARE_TIMEOUT_MS;
	let caseId: string | null = null;
	let quiet = false;

	for (let i = 1; i < argv.length; i += 1) {
		const arg = argv[i];
		const next = argv[i + 1];
		match: switch (arg) {
			case "--concurrency":
				if (!next) usage();
				concurrency = Number(next);
				i += 1;
				break match;
			case "--limit":
				if (!next) usage();
				limit = Number(next);
				i += 1;
				break match;
			case "--profile":
				if (!next) usage();
				profile = next;
				i += 1;
				break match;
			case "--planner":
				if (!next) usage();
				if (!isPlannerChoice(next)) {
					console.error(
						`--planner must be one of: ${PLANNER_CHOICES.join(", ")}`,
					);
					process.exit(2);
				}
				planner = next;
				i += 1;
				break match;
			case "--timeout-ms":
				if (!next) usage();
				timeoutMs = Number(next);
				i += 1;
				break match;
			case "--idle-timeout-ms":
				if (!next) usage();
				idleTimeoutMs = Number(next);
				i += 1;
				break match;
			case "--llm-timeout-ms":
				if (!next) usage();
				llmTimeoutMs = Number(next);
				i += 1;
				break match;
			case "--prepare-timeout-ms":
				if (!next) usage();
				prepareTimeoutMs = Number(next);
				i += 1;
				break match;
			case "--case-id":
				if (!next) usage();
				caseId = next;
				i += 1;
				break match;
			case "--quiet":
				quiet = true;
				break match;
			default:
				console.error(`Unknown argument: ${arg}`);
				usage();
		}
	}

	if (!Number.isFinite(concurrency) || concurrency < 1) {
		console.error("--concurrency must be a positive integer");
		process.exit(2);
	}
	if (limit !== null && (!Number.isFinite(limit) || limit < 1)) {
		console.error("--limit must be a positive integer");
		process.exit(2);
	}
	if (!Number.isFinite(timeoutMs) || timeoutMs < 1) {
		console.error("--timeout-ms must be a positive integer");
		process.exit(2);
	}
	if (!Number.isFinite(idleTimeoutMs) || idleTimeoutMs < 0) {
		console.error("--idle-timeout-ms must be >= 0 (0 disables)");
		process.exit(2);
	}
	if (!Number.isFinite(llmTimeoutMs) || llmTimeoutMs < 1) {
		console.error("--llm-timeout-ms must be a positive integer");
		process.exit(2);
	}
	if (!Number.isFinite(prepareTimeoutMs) || prepareTimeoutMs < 1) {
		console.error("--prepare-timeout-ms must be a positive integer");
		process.exit(2);
	}
	return {
		manifest,
		concurrency,
		limit,
		profile,
		planner,
		timeoutMs,
		idleTimeoutMs,
		llmTimeoutMs,
		prepareTimeoutMs,
		caseId,
		quiet,
	};
}

async function loadManifestCases(manifestPath: string): Promise<ManifestCase[]> {
	const absolute = isAbsolute(manifestPath) ? manifestPath : resolve(REPO_ROOT, manifestPath);
	const text = await readFile(absolute, "utf8");
	const cases: ManifestCase[] = [];
	for (const [lineNo, line] of text.split("\n").entries()) {
		const trimmed = line.trim();
		if (!trimmed) {
			continue;
		}
		const payload = JSON.parse(trimmed) as { case_id?: unknown; project?: unknown };
		if (typeof payload.case_id !== "string" || typeof payload.project !== "string") {
			throw new Error(`Invalid manifest line ${lineNo + 1}: need case_id and project`);
		}
		cases.push({ case_id: payload.case_id, project: payload.project });
	}
	return cases;
}

function runPythonJson<T>(args: string[], timeoutMs: number): Promise<T> {
	return new Promise((resolvePromise, reject) => {
		const signal = AbortSignal.timeout(timeoutMs);
		const child = spawn("uv", ["run", "python", "-m", "agents.pi.hooks", ...args], {
			cwd: REPO_ROOT,
			stdio: ["ignore", "pipe", "pipe"],
			signal,
		});
		let stdout = "";
		let stderr = "";
		child.stdout.on("data", (chunk: Buffer | string) => {
			stdout += chunk.toString();
		});
		child.stderr.on("data", (chunk: Buffer | string) => {
			const text = chunk.toString();
			stderr += text;
			process.stderr.write(text);
		});
		child.on("error", (error) => {
			if (error.name === "AbortError") {
				reject(new Error(`hooks timed out after ${timeoutMs}ms: ${args.join(" ")}`));
				return;
			}
			reject(error);
		});
		child.on("close", (code) => {
			if (signal.aborted) {
				reject(new Error(`hooks timed out after ${timeoutMs}ms: ${args.join(" ")}`));
				return;
			}
			if (code !== 0) {
				reject(new Error(stderr.trim() || `hooks exited with code ${code}`));
				return;
			}
			try {
				resolvePromise(JSON.parse(stdout) as T);
			} catch (error) {
				reject(
					new Error(
						`Failed to parse hooks JSON: ${error instanceof Error ? error.message : String(error)}\n${stdout}`,
					),
				);
			}
		});
	});
}

async function prepareCase(
	caseId: string,
	profile: string,
	manifest: string,
	prepareTimeoutMs: number,
): Promise<PreparePayload> {
	return runPythonJson<PreparePayload>(
		["prepare", "--case-id", caseId, "--profile", profile, "--manifest", manifest],
		prepareTimeoutMs,
	);
}

async function completeCase(
	prepared: PreparePayload,
	stopReason: string,
	usageJson?: string | null,
): Promise<CompletePayload> {
	const args = [
		"complete",
		"--repo-path",
		prepared.repo_path,
		"--elements",
		prepared.elements.join(","),
		"--timeout",
		String(prepared.timeout),
		"--case-id",
		prepared.case_id,
		"--stop-reason",
		stopReason,
		"--profile",
		prepared.profile,
	];
	if (prepared.run_log) {
		args.push("--run-log", prepared.run_log);
	}
	return runPythonJson<CompletePayload>(
		withUsageJsonArg(args, usageJson),
		COMPLETE_TIMEOUT_MS,
	);
}

export function createCaseWaiter(
	eventBus: ReturnType<typeof createEventBus>,
	caseId: string,
	timeoutMs: number,
	onTimeout: () => void,
	graceMs = WALL_TIMEOUT_GRACE_MS,
): {
	promise: Promise<DeepRefactorCompleteEvent>;
	fail: (message: string) => void;
} {
	let settled = false;
	let rejectFn: (error: Error) => void = () => undefined;
	let caseTimer: ReturnType<typeof setTimeout> | undefined;
	let unsubscribeComplete: (() => void) | undefined;
	const startedAtMs = performance.now();

	const cleanup = (): void => {
		if (caseTimer) {
			clearTimeout(caseTimer);
			caseTimer = undefined;
		}
		unsubscribeComplete?.();
		unsubscribeComplete = undefined;
	};

	const promise = new Promise<DeepRefactorCompleteEvent>((resolve, reject) => {
		rejectFn = reject;
		const beginGracePeriod = (): void => {
			if (settled) {
				return;
			}
			const remainingMs = remainingTimeoutDelay(
				startedAtMs,
				timeoutMs,
				performance.now(),
			);
			if (remainingMs > 0) {
				caseTimer = setTimeout(beginGracePeriod, remainingMs);
				return;
			}
			console.error(
				`[wall-timeout] ${caseId}: waiting ${graceMs}ms for completion`,
			);
			caseTimer = setTimeout(() => {
				if (settled) {
					return;
				}
				settled = true;
				cleanup();
				try {
					onTimeout();
				} catch (error) {
					console.error(
						`[wall-timeout] ${caseId} abort failed: ${error instanceof Error ? error.message : String(error)}`,
					);
				}
				reject(new Error(`Timed out waiting for ${COMPLETE_EVENT} (${caseId})`));
			}, graceMs);
		};
		caseTimer = setTimeout(beginGracePeriod, timeoutMs);

		unsubscribeComplete = eventBus.on(COMPLETE_EVENT, (data: unknown) => {
			const event = data as DeepRefactorCompleteEvent;
			if (event.caseId !== caseId || settled) {
				return;
			}
			settled = true;
			cleanup();
			resolve(event);
		});
	});

	return {
		promise,
		fail: (message: string) => {
			if (settled) {
				return;
			}
			settled = true;
			cleanup();
			rejectFn(new Error(message));
		},
	};
}

/**
 * Custom idle watchdog: SDK has httpIdleTimeoutMs (LLM stream only), not
 * "no tool calls for N ms". We abort when session + extension heartbeats go quiet.
 */
function attachIdleWatchdog(
	session: AgentSession,
	eventBus: ReturnType<typeof createEventBus>,
	caseId: string,
	idleTimeoutMs: number,
	onIdle: (reason: string) => void,
): () => void {
	if (idleTimeoutMs <= 0) {
		return () => undefined;
	}

	let timer: ReturnType<typeof setTimeout> | undefined;
	let armed = false;
	let fired = false;
	let lastActivityAtMs = performance.now();

	const clear = (): void => {
		if (timer) {
			clearTimeout(timer);
			timer = undefined;
		}
	};

	const checkIdle = (): void => {
		if (fired || !armed) {
			return;
		}
		const remainingMs = remainingTimeoutDelay(
			lastActivityAtMs,
			idleTimeoutMs,
			performance.now(),
		);
		if (remainingMs > 0) {
			timer = setTimeout(checkIdle, remainingMs);
			return;
		}
		fired = true;
		const reason = `No tool/LLM activity for ${idleTimeoutMs}ms`;
		console.error(`[idle-timeout] ${caseId}: ${reason}`);
		onIdle(reason);
		void session.abort();
	};

	const arm = (): void => {
		armed = true;
		lastActivityAtMs = performance.now();
		clear();
		timer = setTimeout(checkIdle, idleTimeoutMs);
	};

	const unsubSession = session.subscribe((event: AgentSessionEvent) => {
		switch (event.type) {
			case "agent_start":
			case "turn_start":
			case "message_start":
			case "message_update":
			case "message_end":
			case "tool_execution_start":
			case "tool_execution_update":
			case "tool_execution_end":
				arm();
				return;
			case "agent_settled":
				// Between smells the harness is driving next-smell/complete; pause idle kill.
				// Hook timeouts + next_smell_failed completion bound that window.
				armed = false;
				clear();
				return;
			default:
				return;
		}
	});

	const unsubActivity = eventBus.on(ACTIVITY_EVENT, () => {
		if (!armed || fired) {
			return;
		}
		arm();
	});

	return () => {
		fired = true;
		clear();
		unsubSession();
		unsubActivity();
	};
}

async function runCase(
	manifestCase: ManifestCase,
	options: {
		manifest: string;
		profile: string;
		timeoutMs: number;
		idleTimeoutMs: number;
		llmTimeoutMs: number;
		prepareTimeoutMs: number;
		modelRuntime: ModelRuntime;
		model: SelectedModel;
		showActivity: boolean;
		batchAbort: AbortSignal;
		batchAbortController: AbortController;
		onCaseComplete?: (result: CaseResult, runLog: string | null) => void;
	},
): Promise<CaseResult> {
	const {
		manifest,
		profile,
		timeoutMs,
		idleTimeoutMs,
		llmTimeoutMs,
		prepareTimeoutMs,
		modelRuntime,
		model,
		showActivity,
		batchAbort,
		batchAbortController,
		onCaseComplete,
	} = options;
	const caseId = manifestCase.case_id;
	const finishCase = (result: CaseResult, runLog: string | null): CaseResult => {
		onCaseComplete?.(result, runLog);
		return result;
	};
	if (batchAbort.aborted) {
		console.error(`[skip] ${caseId}: batch aborted before start`);
		return finishCase(
			{
				caseId,
				project: manifestCase.project,
				ok: false,
				stopReason: "batch_aborted",
				fixedSmellCount: 0,
				introducedSmellCount: 0,
				fixedIntroducedSmellCount: 0,
				totalIntroducedSmellCount: 0,
				originalSmellCount: 0,
				remainingSmellCount: 0,
				ckError: null,
				error: "batch_aborted",
				repoPath: "",
			},
			null,
		);
	}
	console.log(`[start] ${caseId}`);
	let session: Awaited<ReturnType<typeof createAgentSession>>["session"] | undefined;
	let unsubscribeErrors: (() => void) | undefined;
	let stopIdle: (() => void) | undefined;
	let prepared: PreparePayload | undefined;
	let wallTimedOut = false;
	let completionPromise: Promise<CompletePayload> | undefined;
	let finalizeOnce:
		| ((stopReason: string, usageJson?: string | null) => Promise<CompletePayload>)
		| undefined;
	try {
		prepared = await prepareCase(caseId, profile, manifest, prepareTimeoutMs);
		const preparedCase = prepared;
		finalizeOnce = (
			stopReason: string,
			usageJson?: string | null,
		): Promise<CompletePayload> => {
			if (!completionPromise) {
				completionPromise = completeCase(preparedCase, stopReason, usageJson);
			}
			return completionPromise;
		};
		const eventBus = createEventBus();
		const waiter = createCaseWaiter(eventBus, prepared.case_id, timeoutMs, () => {
			wallTimedOut = true;
			console.error(`[wall-timeout] ${caseId}: aborting session after ${timeoutMs}ms`);
			eventBus.emit(STOP_EVENT, { caseId });
			void session?.abort();
		});
		const loader = new DefaultResourceLoader({
			cwd: prepared.repo_path,
			agentDir: getAgentDir(),
			eventBus,
			noExtensions: true,
			noSkills: true,
			noPromptTemplates: true,
			noContextFiles: true,
			extensionFactories: [
				{
					name: "refactor",
					factory: createDeepRefactorExtension({
						initialCase: prepared,
						manifest,
						batchTerminalEvents: true,
						activityHeartbeatMs: activityHeartbeatInterval(idleTimeoutMs),
					}),
				},
			],
		});
		await loader.reload();

		const created = await createAgentSession({
			cwd: prepared.repo_path,
			agentDir: getAgentDir(),
			model,
			thinkingLevel: THINKING_LEVEL,
			modelRuntime,
			resourceLoader: loader,
			sessionManager: SessionManager.inMemory(prepared.repo_path),
			settingsManager: SettingsManager.inMemory({
				compaction: { enabled: false },
				// Abort LLM HTTP stream if no tokens arrive for this long.
				httpIdleTimeoutMs: idleTimeoutMs > 0 ? idleTimeoutMs : llmTimeoutMs,
				// Overall single LLM request timeout (SDK provider timeoutMs).
				retry: {
					provider: {
						timeoutMs: llmTimeoutMs,
					},
				},
			}),
			tools: ["read", "bash", "edit", "write", "grep", "find", "ls"],
		});
		session = created.session;
		unsubscribeErrors = attachSessionLogging(session, caseId, { showActivity });
		stopIdle = attachIdleWatchdog(session, eventBus, caseId, idleTimeoutMs, (reason) => {
			eventBus.emit(STOP_EVENT, { caseId });
			waiter.fail(`Idle timeout: ${reason}`);
		});
		// Interactive/print/rpc modes call this; without it session_start never fires
		// and initialCase never sends the first smell (agent stays idle until timeout).
		await session.bindExtensions({
			onError: (error) => {
				console.error(
					`[extension-error] ${caseId}: ${error.event} ${error.error}${error.extensionPath ? ` (${error.extensionPath})` : ""}`,
				);
			},
		});

		const event = await waiter.promise;
		const completion = await finalizeOnce(event.stopReason, event.usageJson);
		if (isBatchFatalLlmStop(event.stopReason)) {
			console.error(
				`[batch-abort] fatal LLM failure in ${caseId} (${event.stopReason}); stopping remaining cases`,
			);
			batchAbortController.abort();
		}
		console.log(
			`[done] ${caseId} ${formatSmellProgress({
				fixedSmellCount: completion.fixed_smells,
				originalSmellCount: completion.original_smells,
				fixedIntroducedSmellCount: completion.fixed_introduced_smells,
				totalIntroducedSmellCount: completion.total_introduced_smells,
				introducedSmellCount: completion.introduced_smells,
			})} stop=${completion.stop_reason}`,
		);
		const ok = isCaseOk(
			completion.stop_reason,
			completion.remaining_smells,
			completion.introduced_smells,
			completion.ck_error,
		);
		return finishCase(
			{
				caseId,
				project: manifestCase.project,
				ok,
				stopReason: completion.stop_reason,
				fixedSmellCount: completion.fixed_smells,
				introducedSmellCount: completion.introduced_smells,
				fixedIntroducedSmellCount: completion.fixed_introduced_smells,
				totalIntroducedSmellCount: completion.total_introduced_smells,
				originalSmellCount: completion.original_smells,
				remainingSmellCount: completion.remaining_smells,
				ckError: completion.ck_error,
				error: ok ? null : completion.ck_error ?? completion.stop_reason,
				repoPath: event.repoPath,
			},
			preparedCase.run_log ?? null,
		);
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		const stopReason = wallTimedOut
			? "wall_timeout"
			: message.startsWith("Idle timeout:")
				? "idle_timeout"
				: "error";
		console.error(`[fail] ${caseId}: ${message}`);
		const failedCase = prepared;
		if (failedCase) {
			try {
				await session?.abort();
			} catch (abortError) {
				console.error(
					`[fail] ${caseId} abort failed: ${abortError instanceof Error ? abortError.message : String(abortError)}`,
				);
			}
			try {
				const completion = await (finalizeOnce
					? finalizeOnce(stopReason)
					: completeCase(failedCase, stopReason));
				console.error(
					`[failed-done] ${caseId} ${formatSmellProgress({
						fixedSmellCount: completion.fixed_smells,
						originalSmellCount: completion.original_smells,
						fixedIntroducedSmellCount: completion.fixed_introduced_smells,
						totalIntroducedSmellCount: completion.total_introduced_smells,
						introducedSmellCount: completion.introduced_smells,
					})} stop=${completion.stop_reason}`,
				);
				return finishCase(
					{
						caseId,
						project: manifestCase.project,
						ok: false,
						stopReason: completion.stop_reason,
						fixedSmellCount: completion.fixed_smells,
						introducedSmellCount: completion.introduced_smells,
						fixedIntroducedSmellCount: completion.fixed_introduced_smells,
						totalIntroducedSmellCount: completion.total_introduced_smells,
						originalSmellCount: completion.original_smells,
						remainingSmellCount: completion.remaining_smells,
						ckError: completion.ck_error,
						error: message,
						repoPath: failedCase.repo_path,
					},
					failedCase.run_log ?? null,
				);
			} catch (completionError) {
				console.error(
					`[fail] ${caseId} completion failed: ${completionError instanceof Error ? completionError.message : String(completionError)}`,
				);
			}
		}
		return finishCase(
			{
				caseId,
				project: manifestCase.project,
				ok: false,
				stopReason,
				fixedSmellCount: 0,
				introducedSmellCount: 0,
				fixedIntroducedSmellCount: 0,
				totalIntroducedSmellCount: 0,
				originalSmellCount: failedCase?.smell_count ?? 0,
				remainingSmellCount: failedCase?.smell_count ?? 0,
				ckError: null,
				error: message,
				repoPath: failedCase?.repo_path ?? "",
			},
			failedCase?.run_log ?? null,
		);
	} finally {
		stopIdle?.();
		unsubscribeErrors?.();
		session?.dispose();
	}
}

async function mapPool<T, R>(
	items: T[],
	concurrency: number,
	fn: (item: T) => Promise<R>,
): Promise<R[]> {
	const results: R[] = new Array(items.length);
	let nextIndex = 0;

	async function worker(): Promise<void> {
		while (nextIndex < items.length) {
			const index = nextIndex;
			nextIndex += 1;
			results[index] = await fn(items[index]);
		}
	}

	const workers = Array.from({ length: Math.min(concurrency, items.length) }, () => worker());
	await Promise.all(workers);
	return results;
}

async function main(): Promise<number> {
	applyMiseTomlEnv(REPO_ROOT);
	const args = parseArgs(process.argv.slice(2));
	if (args.planner !== null) {
		process.env.PLANNING_PLANNER = args.planner;
	}
	const manifestPath = isAbsolute(args.manifest)
		? args.manifest
		: resolve(REPO_ROOT, args.manifest);

	let cases = await loadManifestCases(manifestPath);
	if (args.caseId) {
		cases = cases.filter((item) => item.case_id === args.caseId);
		if (cases.length === 0) {
			console.error(`No case with case_id=${JSON.stringify(args.caseId)}`);
			return 1;
		}
	}
	if (args.limit !== null) {
		cases = cases.slice(0, args.limit);
	}

	const batchRunLog = await createBatchRunLog(manifestPath);

	console.log(
		JSON.stringify({
			event: "pi_batch:start",
			manifest: manifestPath,
			cases: cases.length,
			concurrency: args.concurrency,
			profile: args.profile,
			planner: args.planner ?? process.env.PLANNING_PLANNER ?? null,
			weakModel: `${WEAK_MODEL.provider}/${WEAK_MODEL.id}`,
			strongModel: `${STRONG_MODEL.provider}/${STRONG_MODEL.id}`,
			escalateAfterFailedAttempts: 2,
			thinkingLevel: THINKING_LEVEL,
			batchRunLog,
		}),
	);

	const modelRuntime = await ModelRuntime.create();
	await modelRuntime.refresh({ signal: AbortSignal.timeout(15_000) });
	const model = modelRuntime.getModel(WEAK_MODEL.provider, WEAK_MODEL.id);
	if (!model) {
		throw new Error(`Pi model is unavailable: ${WEAK_MODEL.provider}/${WEAK_MODEL.id}`);
	}
	if (!modelRuntime.getModel(STRONG_MODEL.provider, STRONG_MODEL.id)) {
		throw new Error(`Pi model is unavailable: ${STRONG_MODEL.provider}/${STRONG_MODEL.id}`);
	}
	const batchAbortController = new AbortController();
	const finishedResults: CaseResult[] = [];
	const recordCaseComplete = (result: CaseResult, runLog: string | null): void => {
		finishedResults.push(result);
		const resumeTargets = new Set<string>([batchRunLog]);
		if (runLog) {
			resumeTargets.add(projectRelativeRunLog(runLog));
		}
		for (const target of resumeTargets) {
			logManifestResume(target, {
				manifestPath,
				planner: args.planner,
				manifestCases: cases,
				results: finishedResults,
				trigger: "case_end",
				trigger_case_id: result.caseId,
			});
		}
	};
	const results = await mapPool(cases, args.concurrency, (item) =>
		runCase(item, {
			manifest: manifestPath,
			profile: args.profile,
			timeoutMs: args.timeoutMs,
			idleTimeoutMs: args.idleTimeoutMs,
			llmTimeoutMs: args.llmTimeoutMs,
			prepareTimeoutMs: args.prepareTimeoutMs,
			modelRuntime,
			model,
			showActivity: !args.quiet,
			batchAbort: batchAbortController.signal,
			batchAbortController,
			onCaseComplete: recordCaseComplete,
		}),
	);

	logManifestResume(batchRunLog, {
		manifestPath,
		planner: args.planner,
		manifestCases: cases,
		results,
		trigger: "batch_end",
		trigger_case_id: null,
	});

	const failed = results.filter((result) => !result.ok);
	console.log(
		JSON.stringify({
			event: "pi_batch:done",
			cases: results.length,
			failed: failed.length,
			results,
		}),
	);
	return failed.length > 0 ? 1 : 0;
}

if (import.meta.main) {
	const code = await main();
	process.exit(code);
}
