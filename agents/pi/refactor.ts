/**
 * Deep-refactor pi extension
 *
 * Mirrors experiments.main --deep: prepare a dataset case, append the smell-
 * refactoring system prompt, and after every detected *.java source change run
 * the Python verification hook (Maven tests + ORGANIC smells).
 *
 * When the whole case finishes (smells cleared), emits `deep-refactor:complete`
 * and runs the Python `complete` hook (Eliot case summary + CK metrics).
 *
 * Usage (interactive):
 *   pi -e ./agents/pi/refactor.ts
 *   /case <case-id>
 *   /next
 *   /verify
 *   /status
 *   /stop
 *
 * Usage (SDK / batch): createDeepRefactorExtension({ initialCase, manifest })
 *
 * Smells are sent one user message at a time. A smell advances only after the
 * latest Java edit passes tests and removes that exact smell. Unresolved smells
 * get five agent turns before the harness skips them. Transient LLM 429 rate
 * limits wait and resend the same attempt; they do not burn the attempt budget.
 */

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, TextContent } from "@earendil-works/pi-ai";
import type {
	ExtensionAPI,
	ExtensionContext,
	ExtensionFactory,
	ToolResultEvent,
} from "@earendil-works/pi-coding-agent";
import {
	addUsageToTotals,
	createUsageTotals,
	usageTotalsFields,
	usageTurnFields,
	type UsageTotals,
} from "./usage.ts";
import { applyMiseTomlEnv } from "./mise_env.ts";

const EXTENSION_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(EXTENSION_DIR, "../..");
applyMiseTomlEnv(REPO_ROOT);
const STATE_ENTRY = "deep-refactor-state";
const VERIFY_MESSAGE_TYPE = "deep-verify-report";
export const COMPLETE_EVENT = "deep-refactor:complete";
/** Heartbeat while Python hooks run (verify / next-smell) so idle watchdogs do not abort. */
export const ACTIVITY_EVENT = "deep-refactor:activity";
/** Stop the whole refactoring workflow, not only the current Pi operation. */
export const STOP_EVENT = "deep-refactor:stop";
const DEFAULT_PROFILE = "without-planning";
const KNOWN_PROFILES = new Set(["without-planning", "with-planning"]);
const EDIT_TOOLS = new Set(["edit", "write"]);
export const MAX_LLM_TURNS_PER_ATTEMPT = 30;
export const WEAK_MODEL = {
	provider: "openrouter",
	id: "deepseek/deepseek-v4-flash-0731",
} as const;
export const STRONG_MODEL = {
	provider: "openrouter",
	id: "upstage/solar-pro4",
} as const;

/** Parse `/case` args. Case IDs may contain spaces (e.g. project names). */
export function parseDeepCaseArgs(args: string): { caseId: string; profile: string } | null {
	const trimmed = args.trim();
	if (!trimmed) {
		return null;
	}

	const profileFlag = trimmed.match(/^(.*?)\s+--profile\s+(\S+)\s*$/);
	if (profileFlag) {
		const caseId = profileFlag[1].trim();
		return caseId ? { caseId, profile: profileFlag[2] } : null;
	}

	const parts = trimmed.split(/\s+/).filter(Boolean);
	const last = parts[parts.length - 1];
	if (parts.length >= 2 && KNOWN_PROFILES.has(last)) {
		return {
			caseId: parts.slice(0, -1).join(" "),
			profile: last,
		};
	}

	return { caseId: trimmed, profile: DEFAULT_PROFILE };
}

type DeepCaseState = {
	active: boolean;
	caseId: string;
	project: string;
	repoPath: string;
	elements: string[];
	timeout: number;
	profile: string;
	systemPromptAppend: string;
	/** Smell count at /case prepare time. */
	originalSmellCount: number;
	/** Live remaining target smells from the latest ORGANIC pass. */
	remainingSmellCount: number;
	/** How many one-by-one smell tasks have been sent. */
	sentCount: number;
	/** Number of assigned smells confirmed removed by verification. */
	fixedSmellCount: number;
	/** Current ORGANIC findings not present in the baseline smell list. */
	introducedSmellCount: number;
	/** Introduced smells confirmed removed since baseline. */
	fixedIntroducedSmellCount: number;
	/** Introduced smells ever seen during this run (monotonic). */
	totalIntroducedSmellCount: number;
	/** Original task text for the smell handled by the current agent turns. */
	currentSmellTask: string;
	currentSmellKey: string;
	/** Failed-settlement attempt number for the current smell, from 1 through 5. */
	attemptNumber: number;
	/** Assistant turns consumed by the current attempt. */
	attemptTurnCount: number;
	skippedSmellKeys: string[];
	/** Acceptance and presence from the latest verification for this smell. */
	latestVerificationAcceptance: boolean | null;
	currentSmellPresent: boolean | null;
	latestVerificationDiagnostics: string;
	/** Successful Java edits and the edit covered by the latest verification. */
	javaEditRevision: number;
	verifiedJavaEditRevision: number;
	/** Cumulative LLM usage for the whole case. */
	caseUsageTotals: UsageTotals;
	/** LLM usage for the current smell refactoring attempt. */
	refactoringUsageTotals: UsageTotals;
	/** Number of assistant model turns in this case. */
	caseTurnNumber: number;
	/** Per-run Eliot log from prepare. */
	runLog: string;
	runDir: string;
};

export type PreparePayload = {
	case_id: string;
	project: string;
	repo_path: string;
	elements: string[];
	timeout: number;
	profile: string;
	system_prompt_append: string;
	task: string;
	tasks: string[];
	smell_count: number;
	stop_reason: string;
	current_smell_key: string;
	run_dir: string;
	run_log: string;
};

type VerifyPayload = {
	content: string;
	passed: boolean;
	fatal_environment_failure: boolean;
	failure_kind: "jdk" | "maven" | null;
	remaining_smells: number | null;
	fixed_smells: number | null;
	introduced_smells: number | null;
	original_smells: number | null;
	total_introduced_smells: number | null;
	fixed_introduced_smells: number | null;
	current_smell_present: boolean | null;
	tests_acceptable: boolean;
	test_status: string;
	flaky_tests: string[];
};

type NextSmellPayload = {
	task: string;
	smell_key: string;
	remaining_smells: number;
	fixed_smells: number;
	introduced_smells: number;
	original_smells: number;
	total_introduced_smells: number;
	fixed_introduced_smells: number;
	stop_reason: string;
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
	ck: {
		success: boolean;
		exit_code: number;
		classes: number;
		methods: number;
		mean_cbo: number | null;
		mean_lcom: number | null;
		mean_wmc: number | null;
		total_loc: number;
	} | null;
};

export type DeepRefactorOptions = {
	/** Start from a prepare payload (SDK batch); skips /case checkout. */
	initialCase?: PreparePayload;
	/** Optional manifest path forwarded to prepare. */
	manifest?: string;
	/** Let a batch runner execute the Python completion hook exactly once. */
	batchTerminalEvents?: boolean;
	/** Interval for reporting active Python hooks to an idle watchdog. */
	activityHeartbeatMs?: number;
	/** Default bash tool timeout in seconds when the model omits ``timeout``. */
	bashTimeoutSec?: number;
	/** Hard cap for bash tool ``timeout`` (seconds). */
	bashTimeoutMaxSec?: number;
};

/** Default bash timeout when the model does not pass one (seconds). */
export const DEFAULT_BASH_TIMEOUT_SEC = 180;
/** Maximum bash timeout the harness allows (seconds). */
export const MAX_BASH_TIMEOUT_SEC = 300;

/**
 * Force every bash call to a finite timeout so hung Maven/IT cannot block a case.
 * Missing or non-positive values get ``defaultSec``; values above ``maxSec`` are capped.
 */
export function applyBashTimeoutPolicy(
	timeoutSec: number | undefined,
	defaultSec: number = DEFAULT_BASH_TIMEOUT_SEC,
	maxSec: number = MAX_BASH_TIMEOUT_SEC,
): number {
	const ceiling = Math.max(1, maxSec);
	const fallback = Math.min(Math.max(1, defaultSec), ceiling);
	if (timeoutSec === undefined || !Number.isFinite(timeoutSec) || timeoutSec <= 0) {
		return fallback;
	}
	return Math.min(timeoutSec, ceiling);
}

/** True when the bash tool failed because the harness/SDK timeout fired. */
export function isBashTimeoutError(content: string): boolean {
	return /command timed out after\s+\d+\s+seconds/i.test(content);
}

/** Guidance so the agent can roll back and keep trying after a bash timeout. */
export function formatBashTimeoutRecoveryHint(options: {
	streak: number;
	timeoutSec: number;
}): string {
	const streak = Math.max(1, options.streak);
	const lines = [
		"HARNESS NOTE: bash timed out "
			+ `(limit ${options.timeoutSec}s; consecutive timeout #${streak}). `
			+ "The case is still active — you may fix and retry.",
		"Rollback: from the case checkout root, inspect with `git status` / `git diff`, "
			+ "then restore only the named files you changed. Never restore the whole worktree.",
		"Do not re-run long Gradle/integration-test commands via bash. "
			+ "Make a Java change and rely on automatic verification.",
	];
	if (streak >= 2) {
		lines.push(
			"Repeated bash timeouts: prefer an immediate git restore, then a smaller Java fix. "
				+ "Avoid `./gradlew test` and `| tail` loops in bash.",
		);
	}
	return lines.join("\n");
}

export type DeepRefactorCompleteEvent = {
	caseId: string;
	repoPath: string;
	stopReason: string;
	originalSmellCount: number;
	remainingSmellCount: number;
	fixedSmellCount: number;
	introducedSmellCount: number;
	fixedIntroducedSmellCount: number;
	totalIntroducedSmellCount: number;
	profile: string;
	ck: CompletePayload["ck"];
	ckError: string | null;
	/** JSON object for hooks ``--usage-json``; batch runner forwards this to complete. */
	usageJson: string | null;
};

type DeepRefactorStopEvent = {
	caseId: string;
};

function isDeepRefactorStopEvent(data: unknown): data is DeepRefactorStopEvent {
	return (
		typeof data === "object" &&
		data !== null &&
		"caseId" in data &&
		typeof data.caseId === "string"
	);
}

/** Parse remaining_smells=N from verification content when JSON omits it. */
export function remainingFromVerifyContent(content: string): number | null {
	for (const line of content.split("\n")) {
		const trimmed = line.trim();
		if (trimmed.startsWith("remaining_smells=")) {
			const value = Number(trimmed.slice("remaining_smells=".length));
			return Number.isFinite(value) ? value : null;
		}
	}
	return null;
}

export function fixedSmellCount(original: number, remaining: number): number {
	return Math.max(0, original - remaining);
}

export type SmellMetricPayload = {
	remaining_smells: number;
	fixed_smells?: number | null;
	introduced_smells?: number | null;
	original_smells?: number | null;
	total_introduced_smells?: number | null;
	fixed_introduced_smells?: number | null;
};

export function applySmellMetrics(
	current: DeepCaseState,
	metrics: SmellMetricPayload,
): DeepCaseState {
	return {
		...current,
		remainingSmellCount: metrics.remaining_smells,
		fixedSmellCount:
			typeof metrics.fixed_smells === "number"
				? metrics.fixed_smells
				: current.fixedSmellCount,
		introducedSmellCount:
			typeof metrics.introduced_smells === "number"
				? metrics.introduced_smells
				: current.introducedSmellCount,
		fixedIntroducedSmellCount:
			typeof metrics.fixed_introduced_smells === "number"
				? metrics.fixed_introduced_smells
				: current.fixedIntroducedSmellCount,
		totalIntroducedSmellCount:
			typeof metrics.total_introduced_smells === "number"
				? metrics.total_introduced_smells
				: current.totalIntroducedSmellCount,
		originalSmellCount:
			typeof metrics.original_smells === "number"
				? metrics.original_smells
				: current.originalSmellCount,
	};
}

export type LlmFailureKind = "auth" | "provider";

/** Detect transient OpenRouter / upstream rate-limit errors (HTTP 429). */
export function isRateLimitLlmError(detail: string): boolean {
	const haystack = detail.toLowerCase();
	if (haystack.includes("401") || haystack.includes("403") || haystack.includes("402")) {
		return false;
	}
	return (
		haystack.includes("429") ||
		haystack.includes("rate-limited") ||
		haystack.includes("rate limited") ||
		haystack.includes("rate_limit") ||
		haystack.includes("too many requests") ||
		haystack.includes("upstream_provider_shared_pool")
	);
}

/**
 * Delay before resending the same smell after a 429.
 * Prefer ``Retry-After`` / ``retry_after`` when present; else exponential backoff.
 */
export function rateLimitRetryDelayMs(detail: string, streak: number): number {
	const headerMatch = detail.match(/retry-after["\s:=]+(\d+)/i);
	if (headerMatch) {
		const seconds = Number(headerMatch[1]);
		if (Number.isFinite(seconds) && seconds > 0) {
			return Math.min(Math.max(seconds, 1) * 1000, 120_000);
		}
	}
	const jsonMatch = detail.match(/"retry_after"\s*:\s*(\d+)/i);
	if (jsonMatch) {
		const seconds = Number(jsonMatch[1]);
		if (Number.isFinite(seconds) && seconds > 0) {
			return Math.min(Math.max(seconds, 1) * 1000, 120_000);
		}
	}
	const clampedStreak = Math.max(1, Math.min(streak, 6));
	const base = 5_000 * 2 ** (clampedStreak - 1);
	const jitter = Math.floor(Math.random() * 1_000);
	return Math.min(base + jitter, 120_000);
}

/** Classify provider error text from assistant ``stopReason=error`` messages. */
export function classifyLlmError(detail: string): LlmFailureKind | null {
	if (isRateLimitLlmError(detail)) {
		return null;
	}
	const haystack = detail.toLowerCase();
	const authMarkers = [
		"401",
		"403",
		"402",
		"user not found",
		"invalid api key",
		"incorrect api key",
		"no auth credentials",
		"unauthorized",
		"authentication",
		"invalid credentials",
		"insufficient credit",
		"insufficient quota",
		"credit balance",
		"missing bearer",
	];
	if (authMarkers.some((marker) => haystack.includes(marker))) {
		return "auth";
	}
	const providerMarkers = [
		"model not found",
		"no endpoints found",
		"does not exist",
		"not a valid model",
	];
	if (
		providerMarkers.some((marker) => haystack.includes(marker)) ||
		(haystack.includes("404") && haystack.includes("model"))
	) {
		return "provider";
	}
	return null;
}

export function isFatalLlmError(detail: string): boolean {
	return classifyLlmError(detail) !== null;
}

export function llmFailureStopReason(kind: LlmFailureKind): string {
	return kind === "auth" ? "llm_auth_failure" : "llm_provider_failure";
}

export function isBatchFatalLlmStop(stopReason: string): boolean {
	return stopReason === "llm_auth_failure" || stopReason === "llm_provider_failure";
}

/** Turn hook JSON / key=value verify output into a readable multi-line report. */
export function formatVerificationReport(
	result: VerifyPayload,
	options: { caseId: string; repoPath: string; originalSmellCount?: number },
): string {
	const { caseId, repoPath, originalSmellCount } = options;
	let testsFailed: boolean | null = null;
	let remainingSmells =
		typeof result.remaining_smells === "number"
			? result.remaining_smells
			: remainingFromVerifyContent(result.content);
	let verificationError: string | null = null;
	const smellLines: string[] = [];

	for (const line of result.content.split("\n")) {
		const trimmed = line.trim();
		if (!trimmed) {
			continue;
		}
		if (trimmed.startsWith("tests_failed=")) {
			testsFailed = trimmed.slice("tests_failed=".length).toLowerCase() === "true";
			continue;
		}
		if (trimmed.startsWith("remaining_smells=")) {
			const value = Number(trimmed.slice("remaining_smells=".length));
			if (Number.isFinite(value)) {
				remainingSmells = value;
			}
			continue;
		}
		if (trimmed.startsWith("verification_error=")) {
			verificationError = trimmed.slice("verification_error=".length);
			continue;
		}
		if (trimmed === "No remaining target smells.") {
			continue;
		}
		smellLines.push(trimmed);
	}

	const original = originalSmellCount ?? result.original_smells;
	const fixed =
		typeof result.fixed_smells === "number"
			? result.fixed_smells
			: remainingSmells !== null && typeof original === "number"
				? fixedSmellCount(original, remainingSmells)
				: result.fixed_smells;
	const introduced =
		typeof result.introduced_smells === "number" ? result.introduced_smells : 0;
	const totalIntroduced =
		typeof result.total_introduced_smells === "number"
			? result.total_introduced_smells
			: 0;
	const fixedIntroduced =
		typeof result.fixed_introduced_smells === "number"
			? result.fixed_introduced_smells
			: 0;

	const status = result.tests_acceptable ? "ACCEPTABLE" : "FAILED";
	const lines = [
		`Automatic verification — ${status}`,
		`case: ${caseId}`,
		`repo: ${repoPath}`,
		`tests: ${result.test_status || (testsFailed === null ? "unknown" : testsFailed ? "FAILED" : "passed")}`,
		`current smell present: ${
			result.current_smell_present === null
				? "unknown"
				: result.current_smell_present
					? "yes"
					: "no"
		}`,
		`remaining smells: ${remainingSmells === null ? "unknown" : String(remainingSmells)}`,
		`introduced smells remaining: ${String(introduced)}`,
	];
	if (result.flaky_tests.length > 0) {
		lines.push(`timing-flaky tests accepted: ${result.flaky_tests.join(", ")}`);
	}
	if (typeof original === "number" && fixed !== null && fixed !== undefined) {
		lines.push(`original progress: ${fixed}/${original} fixed`);
	}
	if (totalIntroduced > 0) {
		lines.push(`introduced progress: ${fixedIntroduced}/${totalIntroduced} fixed`);
	} else if (introduced > 0) {
		lines.push(`new ORGANIC smells vs baseline: ${introduced}`);
	}
	if (verificationError) {
		lines.push(`error: ${verificationError}`);
	}
	if (
		result.fatal_environment_failure ||
		isToolchainVerificationFailure(result.content, verificationError)
	) {
		lines.push(
			"",
			"HARNESS NOTE: toolchain/environment failure (not a smell-fix issue). " +
				"Do not edit further, do not run Gradle via bash, do not change build files. " +
				"The harness is ending the experiment now.",
		);
	}
	if (smellLines.length > 0) {
		lines.push("", "Remaining target smells:");
		for (const smell of smellLines) {
			lines.push(`  • ${smell}`);
		}
	} else if (testsFailed === false && (remainingSmells === 0 || remainingSmells === null)) {
		lines.push("", "No remaining target smells.");
	}
	return lines.join("\n");
}

/** True when Maven/JDK cannot compile the project regardless of the smell edit. */
export function isToolchainVerificationFailure(
	content: string,
	verificationError: string | null = null,
): boolean {
	const haystack = `${content}\n${verificationError ?? ""}`.toLowerCase();
	const markers = [
		"source option",
		"target option",
		"is no longer supported",
		"invalid target release",
		"invalid source release",
		"release version",
		"no compiler is provided",
		"unsupported class file major version",
		"error: java: error: release version",
	];
	return markers.some((marker) => haystack.includes(marker));
}

function isJavaPath(path: string): boolean {
	return path.toLowerCase().endsWith(".java");
}

function pathUnderRepo(absolutePath: string, repoPath: string): boolean {
	const root = resolve(repoPath);
	const absolute = resolve(absolutePath);
	return absolute === root || absolute.startsWith(`${root}/`);
}

/**
 * True when ``path`` is a ``*.java`` file inside the case checkout.
 *
 * Accepts cwd-relative, repo-relative, absolute, and deepagents-style
 * virtual paths like ``/src/...``.
 */
export function isCaseJavaEdit(path: string, cwd: string, repoPath: string): boolean {
	if (!isJavaPath(path)) {
		return false;
	}
	const stripped = path.replace(/^\/+/, "");
	const candidates = [
		resolve(cwd, path),
		resolve(repoPath, path),
		resolve(repoPath, stripped),
		resolve(path),
	];
	if (isAbsolute(path) && !pathUnderRepo(path, repoPath)) {
		// Virtual FS path: "/src/Main.java" is absolute on disk but means repo-relative.
		candidates.push(resolve(repoPath, stripped));
	}
	return candidates.some((candidate) => pathUnderRepo(candidate, repoPath));
}

function editPathFromToolInput(input: { path?: unknown }): string {
	return typeof input.path === "string" ? input.path : "";
}

function editPathFromEvent(event: ToolResultEvent): string {
	return editPathFromToolInput(event.input as { path?: unknown });
}

/** Block build files; smell refactoring must stay in Java source. */
export function isProtectedBuildPath(path: string): boolean {
	const normalized = path.replace(/\\/g, "/").replace(/^\/+/, "");
	const base = normalized.split("/").pop()?.toLowerCase() ?? "";
	return (
		base === "pom.xml" ||
		base === "build.gradle" ||
		base === "build.gradle.kts" ||
		base === "settings.gradle" ||
		base === "settings.gradle.kts"
	);
}

/** Block only repository-wide rollback; named-file restore remains available. */
export function isBroadGitRestoreCommand(command: string): boolean {
	return (
		/\bgit\s+(?:-\C\s+(?:"[^"]+"|'[^']+'|\S+)\s+)?checkout(?:\s+HEAD)?\s+--\s+\.(?:\s|$|[;&|])/.test(
			command,
		) ||
		/\bgit\s+(?:-\C\s+(?:"[^"]+"|'[^']+'|\S+)\s+)?restore\b[^;&|]*?(?:--\s+)?\.(?:\s|$|[;&|])/.test(
			command,
		)
	);
}

async function javaTreeFingerprint(pi: ExtensionAPI, repoPath: string): Promise<string | null> {
	const javaPathspec = ":(glob)**/*.java";
	const [diff, untracked] = await Promise.all([
		pi.exec(
			"git",
			["diff", "--no-ext-diff", "--binary", "HEAD", "--", javaPathspec],
			{ cwd: repoPath },
		),
		pi.exec(
			"git",
			["ls-files", "-z", "--others", "--exclude-standard", "--", javaPathspec],
			{ cwd: repoPath },
		),
	]);
	if (diff.code !== 0 || untracked.code !== 0) {
		return null;
	}

	const hash = createHash("sha256");
	hash.update(diff.stdout);
	for (const relativePath of untracked.stdout.split("\0").filter(Boolean).sort()) {
		hash.update(relativePath);
		try {
			hash.update(await readFile(resolve(repoPath, relativePath)));
		} catch {
			return null;
		}
	}
	return hash.digest("hex");
}

/** Maven/ORGANIC/CK budget from prepare, plus buffer for process startup. */
function hookTimeoutMs(state: DeepCaseState): number {
	return (state.timeout + 60) * 1000;
}

function combineAbortSignals(...signals: Array<AbortSignal | undefined>): AbortSignal | undefined {
	const active = signals.filter((signal): signal is AbortSignal => signal !== undefined);
	if (active.length === 0) {
		return undefined;
	}
	if (active.length === 1) {
		return active[0];
	}
	return AbortSignal.any(active);
}

function runPythonJson<T>(
	args: string[],
	options: { signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<T> {
	const timeoutSignal =
		options.timeoutMs !== undefined && options.timeoutMs > 0
			? AbortSignal.timeout(options.timeoutMs)
			: undefined;
	const signal = combineAbortSignals(options.signal, timeoutSignal);
	return new Promise((resolvePromise, reject) => {
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
				reject(
					new Error(
						`hooks timed out or aborted after ${options.timeoutMs ?? "?"}ms: ${args.join(" ")}`,
					),
				);
				return;
			}
			reject(error);
		});
		child.on("close", (code) => {
			if (signal?.aborted) {
				reject(
					new Error(
						`hooks timed out or aborted after ${options.timeoutMs ?? "?"}ms: ${args.join(" ")}`,
					),
				);
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

function withRunLog(args: string[], runLog: string): string[] {
	if (!runLog) {
		return args;
	}
	return [...args, "--run-log", runLog];
}

export function logPiEliot(runLog: string, payload: Record<string, unknown>): void {
	const child = spawn(
		"uv",
		[
			"run",
			"python",
			"-m",
			"agents.pi.eliot_log",
			...withRunLog(["--payload", JSON.stringify(payload)], runLog),
		],
		{
			cwd: REPO_ROOT,
			stdio: ["ignore", "ignore", "pipe"],
		},
	);
	child.stderr.on("data", (chunk: Buffer | string) => {
		process.stderr.write(chunk);
	});
	child.on("error", (error) => {
		console.error(
			`[eliot-log] failed: ${error instanceof Error ? error.message : String(error)}`,
		);
	});
}

function defaultUsageState(): Pick<
	DeepCaseState,
	"caseUsageTotals" | "refactoringUsageTotals" | "caseTurnNumber"
> {
	return {
		caseUsageTotals: createUsageTotals(),
		refactoringUsageTotals: createUsageTotals(),
		caseTurnNumber: 0,
	};
}

function recordAssistantTurn(
	current: DeepCaseState,
	message: AssistantMessage,
): DeepCaseState {
	const turnNumber = current.caseTurnNumber + 1;
	const next: DeepCaseState = {
		...current,
		caseTurnNumber: turnNumber,
		attemptTurnCount: current.attemptTurnCount + 1,
		caseUsageTotals: addUsageToTotals(current.caseUsageTotals, message.usage),
		refactoringUsageTotals: addUsageToTotals(
			current.refactoringUsageTotals,
			message.usage,
		),
	};
	logPiEliot(current.runLog, {
		message_type: "pi:llm_turn",
		case_id: current.caseId,
		smell_key: current.currentSmellKey,
		attempt_number: current.attemptNumber,
		turn_number: turnNumber,
		model: message.model,
		provider: message.provider,
		stop_reason: message.stopReason,
		...usageTurnFields(message.usage),
	});
	return next;
}

function logRefactoringComplete(current: DeepCaseState, smellKey: string): void {
	logPiEliot(current.runLog, {
		message_type: "pi:refactoring_complete",
		case_id: current.caseId,
		smell_key: smellKey,
		attempt_number: current.attemptNumber,
		fixed_smell_count: current.fixedSmellCount,
		...usageTotalsFields(current.refactoringUsageTotals),
	});
}

function logCaseCost(current: DeepCaseState, stopReason: string): void {
	logPiEliot(current.runLog, {
		message_type: "pi:case_cost",
		case_id: current.caseId,
		stop_reason: stopReason,
		fixed_smell_count: current.fixedSmellCount,
		original_smell_count: current.originalSmellCount,
		remaining_smell_count: current.remainingSmellCount,
		...usageTotalsFields(current.caseUsageTotals),
	});
}

function textFromContent(content: ToolResultEvent["content"]): string {
	return content
		.filter((part): part is { type: "text"; text: string } => part.type === "text")
		.map((part) => part.text)
		.join("\n");
}

function userMessageText(message: AgentMessage): string {
	if (message.role !== "user") {
		return "";
	}
	const content = message.content;
	if (typeof content === "string") {
		return content;
	}
	if (Array.isArray(content)) {
		return content
			.filter((part): part is TextContent => part.type === "text")
			.map((part) => part.text)
			.join("\n");
	}
	return "";
}

/** True for harness-generated one-by-one smell task user messages. */
export function isSmellTaskMessage(message: AgentMessage): boolean {
	const text = userMessageText(message);
	return (
		text.includes("Smell ") &&
		text.includes(" of ") &&
		text.includes("Refactoring advice (") &&
		text.includes("Focus only on this smell")
	);
}

/** Keep only the current smell turn: last smell task plus any follow-up in that turn. */
export function filterSmellTaskContext(messages: AgentMessage[]): AgentMessage[] {
	let lastSmellIndex = -1;
	for (let index = messages.length - 1; index >= 0; index -= 1) {
		if (isSmellTaskMessage(messages[index])) {
			lastSmellIndex = index;
			break;
		}
	}
	if (lastSmellIndex < 0) {
		return messages;
	}
	return messages.slice(lastSmellIndex);
}

type UiSink = Pick<ExtensionContext, "abort" | "hasUI" | "ui">;

export function modelForAttempt(attemptNumber: number): typeof WEAK_MODEL | typeof STRONG_MODEL {
	return attemptNumber <= 2 ? WEAK_MODEL : STRONG_MODEL;
}

export function shouldAbortAttempt(turnCount: number, stopReason: string): boolean {
	return turnCount >= MAX_LLM_TURNS_PER_ATTEMPT && stopReason === "toolUse";
}

async function selectRefactorModel(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	modelRef: typeof WEAK_MODEL | typeof STRONG_MODEL,
): Promise<boolean> {
	const model = ctx.modelRegistry.find(modelRef.provider, modelRef.id);
	if (!model) {
		console.error(`[model-select] unavailable: ${modelRef.provider}/${modelRef.id}`);
		return false;
	}
	const selected = await pi.setModel(model);
	if (!selected) {
		console.error(`[model-select] no credentials: ${modelRef.provider}/${modelRef.id}`);
		return false;
	}
	return true;
}

function runCaseVerification(
	state: DeepCaseState,
	signal?: AbortSignal,
): Promise<VerifyPayload> {
	return runPythonJson<VerifyPayload>(
		withRunLog(
			[
				"verify",
				"--repo-path",
				state.repoPath,
				"--elements",
				state.elements.join(","),
				"--timeout",
				String(state.timeout),
				"--case-id",
				state.caseId,
				"--current-smell-key",
				state.currentSmellKey,
			],
			state.runLog,
		),
		{ signal, timeoutMs: hookTimeoutMs(state) },
	);
}

function runNextSmell(state: DeepCaseState, signal?: AbortSignal): Promise<NextSmellPayload> {
	return runPythonJson<NextSmellPayload>(
		withRunLog(
			[
				"next-smell",
				"--repo-path",
				state.repoPath,
				"--elements",
				state.elements.join(","),
				"--case-id",
				state.caseId,
				"--skip-smell-keys",
				JSON.stringify(state.skippedSmellKeys),
			],
			state.runLog,
		),
		{ signal, timeoutMs: hookTimeoutMs(state) },
	);
}

function runCaseComplete(
	state: DeepCaseState,
	stopReason: string,
	signal?: AbortSignal,
): Promise<CompletePayload> {
	const args = withRunLog(
		[
			"complete",
			"--repo-path",
			state.repoPath,
			"--elements",
			state.elements.join(","),
			"--timeout",
			String(state.timeout),
			"--case-id",
			state.caseId,
			"--stop-reason",
			stopReason,
			"--profile",
			state.profile,
			"--usage-json",
			JSON.stringify(usageTotalsFields(state.caseUsageTotals)),
		],
		state.runLog,
	);
	return runPythonJson<CompletePayload>(args, {
		signal,
		timeoutMs: hookTimeoutMs(state),
	});
}

export function createDeepRefactorExtension(
	options: DeepRefactorOptions = {},
): ExtensionFactory {
	return (pi: ExtensionAPI) => {
		deepRefactorExtension(pi, options);
	};
}

export default function deepRefactorExtension(
	pi: ExtensionAPI,
	options: DeepRefactorOptions = {},
): void {
	let state: DeepCaseState | null = null;
	let sendingSmell = false;
	let advancingSmell = false;
	let completionReported = false;
	let externallyStopped = false;
	let bashTimeoutStreak = 0;
	let lastLlmRateLimitDetail: string | null = null;
	let rateLimitStreak = 0;
	const javaFingerprintBeforeBash = new Map<string, string | null>();
	const activityHeartbeatMs = options.activityHeartbeatMs ?? 10_000;
	const bashTimeoutSec = options.bashTimeoutSec ?? DEFAULT_BASH_TIMEOUT_SEC;
	const bashTimeoutMaxSec = options.bashTimeoutMaxSec ?? MAX_BASH_TIMEOUT_SEC;

	function persistState(next: DeepCaseState | null): void {
		state = next;
		if (next) {
			pi.appendEntry(STATE_ENTRY, next);
		} else {
			pi.appendEntry(STATE_ENTRY, { active: false });
		}
	}

	function statusLabel(current: DeepCaseState): string {
		const fixed = current.fixedSmellCount;
		const introducedRemaining = current.introducedSmellCount;
		const smell = current.currentSmellKey || "none";
		const attempt = current.attemptNumber > 0 ? ` attempt ${current.attemptNumber}/5` : "";
		const introducedLabel =
			current.totalIntroducedSmellCount > 0
				? `, ${current.fixedIntroducedSmellCount}/${current.totalIntroducedSmellCount} introduced`
				: introducedRemaining > 0
					? `, ${introducedRemaining} introduced remaining`
					: "";
		return `deep: ${current.caseId} (${fixed}/${current.originalSmellCount} original${introducedLabel}, smell ${smell}${attempt})`;
	}

	function applyRemainingCount(remaining: number): DeepCaseState | null {
		if (!state?.active) {
			return null;
		}
		const next: DeepCaseState = {
			...state,
			remainingSmellCount: Math.max(0, remaining),
		};
		persistState(next);
		return next;
	}

	function verificationDiagnostics(result: VerifyPayload): string {
		const smellStatus =
			result.current_smell_present === null
				? "current smell status unknown"
				: result.current_smell_present
					? "current smell still present"
					: "current smell removed";
		const flaky =
			result.flaky_tests.length > 0 ? `; accepted timing flakes: ${result.flaky_tests.join(", ")}` : "";
		return `tests ${result.test_status || (result.tests_acceptable ? "acceptable" : "not acceptable")}; ${smellStatus}${flaky}`;
	}

	function verificationGateSatisfied(current: DeepCaseState): boolean {
		return (
			current.javaEditRevision > 0 &&
			current.verifiedJavaEditRevision === current.javaEditRevision &&
			current.latestVerificationAcceptance === true &&
			current.currentSmellPresent === false
		);
	}

	function applyVerificationResult(
		result: VerifyPayload,
		expectedSmellKey: string,
		editRevision: number,
	): DeepCaseState | null {
		if (!state?.active) {
			return null;
		}
		const remaining =
			typeof result.remaining_smells === "number"
				? result.remaining_smells
				: remainingFromVerifyContent(result.content);
		if (
			state.currentSmellKey !== expectedSmellKey ||
			state.javaEditRevision !== editRevision
		) {
			return state;
		}
		const smellFixed =
			result.tests_acceptable &&
			result.current_smell_present === false &&
			state.currentSmellPresent !== false;
		const metricsState =
			remaining === null
				? state
				: applySmellMetrics(state, {
						remaining_smells: remaining,
						fixed_smells: result.fixed_smells,
						introduced_smells: result.introduced_smells,
						original_smells: result.original_smells,
					});
		if (smellFixed) {
			logRefactoringComplete(metricsState, expectedSmellKey);
		}
		const next: DeepCaseState = {
			...metricsState,
			latestVerificationAcceptance: result.tests_acceptable,
			currentSmellPresent: result.current_smell_present,
			latestVerificationDiagnostics: verificationDiagnostics(result),
			verifiedJavaEditRevision: editRevision,
			refactoringUsageTotals: smellFixed
				? createUsageTotals()
				: state.refactoringUsageTotals,
		};
		persistState(next);
		return next;
	}

	async function reportCompletion(
		current: DeepCaseState,
		stopReason: string,
		ctx: UiSink,
	): Promise<void> {
		if (completionReported || externallyStopped) {
			return;
		}
		completionReported = true;
		logCaseCost(current, stopReason);
		if (options.batchTerminalEvents) {
			pi.events.emit(COMPLETE_EVENT, {
				caseId: current.caseId,
				repoPath: current.repoPath,
				stopReason,
				originalSmellCount: current.originalSmellCount,
				remainingSmellCount: current.remainingSmellCount,
				fixedSmellCount: current.fixedSmellCount,
				introducedSmellCount: current.introducedSmellCount,
				fixedIntroducedSmellCount: current.fixedIntroducedSmellCount,
				totalIntroducedSmellCount: current.totalIntroducedSmellCount,
				profile: current.profile,
				ck: null,
				ckError: null,
				usageJson: JSON.stringify(usageTotalsFields(current.caseUsageTotals)),
			} satisfies DeepRefactorCompleteEvent);
			persistState({ ...current, active: false });
			return;
		}
		pi.events.emit(ACTIVITY_EVENT, { phase: "complete", caseId: current.caseId });
		const heartbeat = setInterval(() => {
			pi.events.emit(ACTIVITY_EVENT, { phase: "complete", caseId: current.caseId });
		}, activityHeartbeatMs);
		try {
			const payload = await runCaseComplete(current, stopReason);
			if (externallyStopped) {
				return;
			}
			const updated = applySmellMetrics(current, payload);
			const remaining = updated.remainingSmellCount;
			const event: DeepRefactorCompleteEvent = {
				caseId: updated.caseId,
				repoPath: updated.repoPath,
				stopReason: payload.stop_reason || stopReason,
				originalSmellCount: updated.originalSmellCount,
				remainingSmellCount: remaining,
				fixedSmellCount: updated.fixedSmellCount,
				introducedSmellCount: updated.introducedSmellCount,
				fixedIntroducedSmellCount: updated.fixedIntroducedSmellCount,
				totalIntroducedSmellCount: updated.totalIntroducedSmellCount,
				profile: updated.profile,
				ck: payload.ck,
				ckError: payload.ck_error,
				usageJson: JSON.stringify(usageTotalsFields(updated.caseUsageTotals)),
			};
			pi.events.emit(COMPLETE_EVENT, event);
			persistState({ ...updated, active: false });
			if (ctx.hasUI) {
				ctx.ui.setStatus("deep-refactor", statusLabel(updated));
				ctx.ui.notify(
					`Case complete: ${updated.caseId} (${event.fixedSmellCount}/${updated.originalSmellCount} original, ${event.fixedIntroducedSmellCount}/${event.totalIntroducedSmellCount} introduced)`,
					"info",
				);
			}
		} catch (error) {
			if (externallyStopped) {
				return;
			}
			const message = error instanceof Error ? error.message : String(error);
			const event: DeepRefactorCompleteEvent = {
				caseId: current.caseId,
				repoPath: current.repoPath,
				stopReason,
				originalSmellCount: current.originalSmellCount,
				remainingSmellCount: current.remainingSmellCount,
				fixedSmellCount: current.fixedSmellCount,
				introducedSmellCount: current.introducedSmellCount,
				fixedIntroducedSmellCount: current.fixedIntroducedSmellCount,
				totalIntroducedSmellCount: current.totalIntroducedSmellCount,
				profile: current.profile,
				ck: null,
				ckError: message,
				usageJson: JSON.stringify(usageTotalsFields(current.caseUsageTotals)),
			};
			pi.events.emit(COMPLETE_EVENT, event);
			persistState({ ...current, active: false });
			if (ctx.hasUI) {
				ctx.ui.notify(`Case complete metrics failed: ${message}`, "error");
			}
		} finally {
			clearInterval(heartbeat);
		}
	}

	function reportFatalEnvironmentFailure(
		current: DeepCaseState,
		result: VerifyPayload,
		ctx: UiSink,
	): void {
		if (completionReported) {
			return;
		}
		completionReported = true;
		persistState({ ...current, active: false });
		const failureKind = result.failure_kind ?? "environment";
		console.error(
			`[fatal-${failureKind}] ${current.caseId}: ending experiment\n${result.content}`,
		);
		pi.events.emit(COMPLETE_EVENT, {
			caseId: current.caseId,
			repoPath: current.repoPath,
			stopReason: `${failureKind}_failure`,
			originalSmellCount: current.originalSmellCount,
			remainingSmellCount: current.remainingSmellCount,
			fixedSmellCount: current.fixedSmellCount,
			introducedSmellCount: current.introducedSmellCount,
			fixedIntroducedSmellCount: current.fixedIntroducedSmellCount,
			totalIntroducedSmellCount: current.totalIntroducedSmellCount,
			profile: current.profile,
			ck: null,
			ckError: null,
			usageJson: JSON.stringify(usageTotalsFields(current.caseUsageTotals)),
		} satisfies DeepRefactorCompleteEvent);
		if (ctx.hasUI) {
			ctx.ui.setStatus("deep-refactor", `deep: fatal ${failureKind} failure`);
			ctx.ui.notify(
				`${failureKind.toUpperCase()} failure: experiment stopped`,
				"error",
			);
		}
	}

	async function reportFatalLlmFailure(
		current: DeepCaseState,
		detail: string,
		kind: LlmFailureKind,
		ctx: UiSink,
	): Promise<void> {
		if (completionReported || !current.active) {
			return;
		}
		persistState({ ...current, active: false });
		ctx.abort();
		const stopReason = llmFailureStopReason(kind);
		console.error(`[fatal-llm] ${current.caseId} (${kind}): ${detail}`);
		logPiEliot(current.runLog, {
			message_type: "pi:fatal_llm_failure",
			case_id: current.caseId,
			failure_kind: kind,
			detail,
			stop_reason: stopReason,
		});
		if (ctx.hasUI) {
			ctx.ui.setStatus("deep-refactor", `deep: fatal ${kind} llm failure`);
			ctx.ui.notify(`LLM ${kind} failure: experiment stopped`, "error");
		}
		await reportCompletion(current, stopReason, ctx);
	}

	function stateFromPrepare(payload: PreparePayload): DeepCaseState {
		return {
			active: true,
			caseId: payload.case_id,
			project: payload.project,
			repoPath: payload.repo_path,
			elements: payload.elements,
			timeout: payload.timeout,
			profile: payload.profile,
			systemPromptAppend: payload.system_prompt_append,
			originalSmellCount: payload.smell_count,
			remainingSmellCount: payload.smell_count,
			sentCount: 0,
			fixedSmellCount: 0,
			introducedSmellCount: 0,
			fixedIntroducedSmellCount: 0,
			totalIntroducedSmellCount: 0,
			currentSmellTask: "",
			currentSmellKey: "",
			attemptNumber: 0,
			attemptTurnCount: 0,
			skippedSmellKeys: [],
			latestVerificationAcceptance: null,
			currentSmellPresent: null,
			latestVerificationDiagnostics: "not verified",
			javaEditRevision: 0,
			verifiedJavaEditRevision: -1,
			runLog: payload.run_log,
			runDir: payload.run_dir,
			...defaultUsageState(),
		};
	}

	async function sendNewSmell(
		task: string,
		smellKey: string,
		ctx: ExtensionContext,
		remaining: number,
	): Promise<boolean> {
		if (externallyStopped || !state?.active || !task || !smellKey) {
			return false;
		}
		await selectRefactorModel(pi, ctx, WEAK_MODEL);
		const next: DeepCaseState = {
			...state,
			remainingSmellCount: remaining,
			sentCount: state.sentCount + 1,
			currentSmellTask: task,
			currentSmellKey: smellKey,
			attemptNumber: 1,
			attemptTurnCount: 0,
			latestVerificationAcceptance: null,
			currentSmellPresent: null,
			latestVerificationDiagnostics: "not verified after a Java edit",
			javaEditRevision: 0,
			verifiedJavaEditRevision: -1,
			refactoringUsageTotals: createUsageTotals(),
		};
		rateLimitStreak = 0;
		lastLlmRateLimitDetail = null;
		persistState(next);
		sendingSmell = true;
		pi.sendUserMessage(task);
		if (ctx.hasUI) {
			ctx.ui.setStatus("deep-refactor", statusLabel(next));
			const fixed = next.fixedSmellCount;
			const introducedRemaining = next.introducedSmellCount;
			const introducedLabel =
				next.totalIntroducedSmellCount > 0
					? `, ${next.fixedIntroducedSmellCount}/${next.totalIntroducedSmellCount} introduced`
					: introducedRemaining > 0
						? `, ${introducedRemaining} introduced remaining`
						: "";
			ctx.ui.notify(
				`Sent smell ${smellKey}, attempt 1/5 (${fixed}/${next.originalSmellCount} original${introducedLabel}, ${next.remainingSmellCount} left)`,
				"info",
			);
		}
		return true;
	}

	function retryCurrentSmell(
		ctx: UiSink,
		options: {
			consumeAttempt?: boolean;
			rateLimitDetail?: string | null;
		} = {},
	): boolean {
		const consumeAttempt = options.consumeAttempt ?? true;
		const rateLimitDetail = options.rateLimitDetail ?? null;
		if (
			externallyStopped ||
			!state?.active ||
			!state.currentSmellTask ||
			(consumeAttempt && state.attemptNumber >= 5)
		) {
			return false;
		}
		const attemptNumber = consumeAttempt
			? state.attemptNumber + 1
			: state.attemptNumber;
		const retryLines = [state.currentSmellTask, ""];
		if (consumeAttempt) {
			retryLines.push(
				`Retry attempt ${attemptNumber}/5 for the same smell.`,
				`Prior verification: ${state.latestVerificationDiagnostics}.`,
				"Your prior edits remain in the worktree. Inspect `git diff` before changing anything.",
				"Do not repeat a strategy that passed tests but left the current smell present.",
				"Restore only named broken files; never restore the whole worktree.",
				"Do not move to another smell. Make a Java edit and rely on its automatic verification.",
			);
		} else {
			retryLines.push(
				`LLM rate limit (HTTP 429). Same smell attempt ${attemptNumber}/5 — this does not count as a failed attempt.`,
				rateLimitDetail
					? `Provider detail: ${rateLimitDetail.slice(0, 400)}`
					: "Wait finished; continue the same smell.",
				"Do not move to another smell. Make a Java edit and rely on its automatic verification.",
			);
		}
		const next: DeepCaseState = {
			...state,
			attemptNumber,
			attemptTurnCount: consumeAttempt ? 0 : state.attemptTurnCount,
			sentCount: state.sentCount + 1,
		};
		persistState(next);
		sendingSmell = true;
		pi.sendUserMessage(retryLines.join("\n"));
		if (ctx.hasUI) {
			ctx.ui.setStatus("deep-refactor", statusLabel(next));
			ctx.ui.notify(
				consumeAttempt
					? `Retrying smell ${next.currentSmellKey}, attempt ${attemptNumber}/5`
					: `Rate-limit wait done; resending smell ${next.currentSmellKey} (still attempt ${attemptNumber}/5)`,
				consumeAttempt ? "warning" : "info",
			);
		}
		return true;
	}

	function skipCurrentSmell(ctx: UiSink): DeepCaseState | null {
		if (!state?.active || !state.currentSmellKey) {
			return null;
		}
		const skippedSmellKeys = state.skippedSmellKeys.includes(state.currentSmellKey)
			? state.skippedSmellKeys
			: [...state.skippedSmellKeys, state.currentSmellKey];
		const next: DeepCaseState = { ...state, skippedSmellKeys };
		persistState(next);
		console.warn(
			`[smell-skipped] ${next.caseId}: ${next.currentSmellKey} after ${next.attemptNumber} attempts`,
		);
		if (ctx.hasUI) {
			ctx.ui.notify(
				`Skipped smell ${next.currentSmellKey} after ${next.attemptNumber} failed attempts`,
				"warning",
			);
		}
		return next;
	}

	async function sendNextSmell(
		ctx: ExtensionContext,
		options: { source: "manual" | "auto" },
	): Promise<boolean> {
		if (externallyStopped || !state?.active || sendingSmell || advancingSmell) {
			return false;
		}
		advancingSmell = true;
		pi.events.emit(ACTIVITY_EVENT, { phase: "next_smell", caseId: state.caseId });
		const heartbeat = setInterval(() => {
			pi.events.emit(ACTIVITY_EVENT, { phase: "next_smell", caseId: state?.caseId });
		}, activityHeartbeatMs);
		try {
			const payload = await runNextSmell(state);
			if (externallyStopped || !state?.active) {
				return false;
			}
			const updated = applySmellMetrics(state, payload);
			persistState(updated);
			if (
				payload.stop_reason === "smells_cleared" ||
				payload.stop_reason === "smells_exhausted" ||
				!payload.task
			) {
				await reportCompletion(updated, payload.stop_reason || "smells_cleared", ctx);
				return false;
			}
			return await sendNewSmell(
				payload.task,
				payload.smell_key,
				ctx,
				payload.remaining_smells,
			);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			console.error(`[next-smell] ${state.caseId}: ${message}`);
			if (ctx.hasUI) {
				ctx.ui.notify(
					options.source === "auto"
						? `Auto next-smell failed: ${message}`
						: `next failed: ${message}`,
					"error",
				);
			}
			await reportCompletion(state, "next_smell_failed", ctx);
			return false;
		} finally {
			clearInterval(heartbeat);
			advancingSmell = false;
		}
	}

	async function handleSettledTurn(
		ctx: ExtensionContext,
		source: "manual" | "auto",
	): Promise<void> {
		if (externallyStopped || !state?.active || sendingSmell || !state.currentSmellTask) {
			return;
		}
		if (verificationGateSatisfied(state)) {
			rateLimitStreak = 0;
			lastLlmRateLimitDetail = null;
			await sendNextSmell(ctx, { source });
			return;
		}
		if (lastLlmRateLimitDetail !== null) {
			const detail = lastLlmRateLimitDetail;
			lastLlmRateLimitDetail = null;
			rateLimitStreak += 1;
			const delayMs = rateLimitRetryDelayMs(detail, rateLimitStreak);
			console.error(
				`[llm-rate-limit] ${state.caseId}: waiting ${delayMs}ms then resending attempt ${state.attemptNumber}/5 (streak=${rateLimitStreak})`,
			);
			if (ctx.hasUI) {
				ctx.ui.notify(
					`LLM rate-limited; waiting ${Math.ceil(delayMs / 1000)}s then retrying same attempt`,
					"warning",
				);
			}
			pi.events.emit(ACTIVITY_EVENT, {
				phase: "llm_rate_limit_wait",
				caseId: state.caseId,
			});
			const heartbeat = setInterval(() => {
				pi.events.emit(ACTIVITY_EVENT, {
					phase: "llm_rate_limit_wait",
					caseId: state?.caseId,
				});
			}, activityHeartbeatMs);
			try {
				await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
			} finally {
				clearInterval(heartbeat);
			}
			if (externallyStopped || !state?.active) {
				return;
			}
			retryCurrentSmell(ctx, {
				consumeAttempt: false,
				rateLimitDetail: detail,
			});
			return;
		}
		rateLimitStreak = 0;
		if (state.attemptNumber < 5) {
			if (state.attemptNumber === 2) {
				const selected = await selectRefactorModel(pi, ctx, STRONG_MODEL);
				if (ctx.hasUI) {
					ctx.ui.notify(
						selected
							? `Escalated to ${STRONG_MODEL.id} after two failed attempts`
							: `Could not select ${STRONG_MODEL.id}; keeping the current model`,
						selected ? "info" : "warning",
					);
				}
			}
			retryCurrentSmell(ctx);
			return;
		}
		if (skipCurrentSmell(ctx)) {
			await sendNextSmell(ctx, { source });
		}
	}

	pi.on("session_start", async (_event, ctx) => {
		externallyStopped = false;
		for (const entry of ctx.sessionManager.getEntries()) {
			if (entry.type === "custom" && entry.customType === STATE_ENTRY) {
				const data = entry.data as DeepCaseState | { active: false };
				if (data && "active" in data && data.active && "caseId" in data) {
					const original =
						typeof data.originalSmellCount === "number"
							? data.originalSmellCount
							: typeof (data as { smellTasks?: string[] }).smellTasks?.length === "number"
								? (data as { smellTasks: string[] }).smellTasks.length
								: 0;
					const remaining =
						typeof data.remainingSmellCount === "number" ? data.remainingSmellCount : original;
					state = {
						...data,
						originalSmellCount: original,
						remainingSmellCount: remaining,
						sentCount: typeof data.sentCount === "number" ? data.sentCount : 0,
						fixedSmellCount:
							typeof data.fixedSmellCount === "number" ? data.fixedSmellCount : 0,
						introducedSmellCount:
							typeof data.introducedSmellCount === "number"
								? data.introducedSmellCount
								: 0,
						fixedIntroducedSmellCount:
							typeof data.fixedIntroducedSmellCount === "number"
								? data.fixedIntroducedSmellCount
								: 0,
						totalIntroducedSmellCount:
							typeof data.totalIntroducedSmellCount === "number"
								? data.totalIntroducedSmellCount
								: 0,
						currentSmellTask:
							typeof data.currentSmellTask === "string" ? data.currentSmellTask : "",
						currentSmellKey:
							typeof data.currentSmellKey === "string" ? data.currentSmellKey : "",
						attemptNumber:
							typeof data.attemptNumber === "number" ? data.attemptNumber : 0,
						attemptTurnCount:
							typeof data.attemptTurnCount === "number" ? data.attemptTurnCount : 0,
						skippedSmellKeys: Array.isArray(data.skippedSmellKeys)
							? data.skippedSmellKeys.filter(
									(key): key is string => typeof key === "string",
								)
							: [],
						latestVerificationAcceptance:
							typeof data.latestVerificationAcceptance === "boolean"
								? data.latestVerificationAcceptance
								: null,
						currentSmellPresent:
							typeof data.currentSmellPresent === "boolean"
								? data.currentSmellPresent
								: null,
						latestVerificationDiagnostics:
							typeof data.latestVerificationDiagnostics === "string"
								? data.latestVerificationDiagnostics
								: "not verified",
						javaEditRevision:
							typeof data.javaEditRevision === "number" ? data.javaEditRevision : 0,
						verifiedJavaEditRevision:
							typeof data.verifiedJavaEditRevision === "number"
								? data.verifiedJavaEditRevision
								: -1,
						...defaultUsageState(),
						...(typeof data.caseUsageTotals === "object" && data.caseUsageTotals
							? { caseUsageTotals: data.caseUsageTotals as UsageTotals }
							: {}),
						...(typeof data.refactoringUsageTotals === "object" &&
						data.refactoringUsageTotals
							? {
									refactoringUsageTotals:
										data.refactoringUsageTotals as UsageTotals,
								}
							: {}),
						caseTurnNumber:
							typeof data.caseTurnNumber === "number" ? data.caseTurnNumber : 0,
						runLog: typeof data.runLog === "string" ? data.runLog : "",
						runDir: typeof data.runDir === "string" ? data.runDir : "",
					};
					completionReported = false;
					if (ctx.hasUI) {
						ctx.ui.setStatus("deep-refactor", statusLabel(state));
					}
					return;
				}
			}
		}

		const initial = options.initialCase;
		if (!initial) {
			return;
		}
		completionReported = false;
		if (initial.stop_reason === "smells_cleared" || initial.smell_count === 0) {
			const empty = stateFromPrepare(initial);
			persistState(empty);
			await reportCompletion(empty, "smells_cleared", ctx);
			return;
		}
		const nextState = stateFromPrepare(initial);
		persistState(nextState);
		pi.setSessionName(`deep:${initial.case_id}`);
		if (ctx.hasUI) {
			ctx.ui.setStatus("deep-refactor", statusLabel(nextState));
		}
		const firstTask =
			(Array.isArray(initial.tasks) && initial.tasks[0]) || initial.task || "";
		if (!firstTask) {
			await reportCompletion(nextState, "smells_cleared", ctx);
			return;
		}
		await sendNewSmell(
			firstTask,
			initial.current_smell_key,
			ctx,
			initial.smell_count,
		);
	});

	pi.on("before_agent_start", async (event) => {
		if (!state?.active || !state.systemPromptAppend) {
			return undefined;
		}
		return {
			systemPrompt: `${event.systemPrompt}\n\n${state.systemPromptAppend}`,
		};
	});

	pi.on("context", async (event) => {
		if (!state?.active) {
			return undefined;
		}
		return {
			messages: filterSmellTaskContext(event.messages),
		};
	});

	pi.on("tool_call", async (event) => {
		if (event.toolName === "bash") {
			const command = typeof event.input.command === "string" ? event.input.command : "";
			if (state?.active && isBroadGitRestoreCommand(command)) {
				return {
					block: true,
					reason:
						"Deep-refactor blocks whole-worktree rollback because it erases valid edits. " +
						"Inspect `git diff`, then restore only named files.",
				};
			}
			if (state?.active) {
				javaFingerprintBeforeBash.set(
					event.toolCallId,
					await javaTreeFingerprint(pi, state.repoPath),
				);
			}
			event.input.timeout = applyBashTimeoutPolicy(
				event.input.timeout,
				bashTimeoutSec,
				bashTimeoutMaxSec,
			);
			return undefined;
		}
		if (!state?.active || !EDIT_TOOLS.has(event.toolName)) {
			return undefined;
		}
		const path = editPathFromToolInput(event.input as { path?: unknown });
		if (!path || !isProtectedBuildPath(path)) {
			return undefined;
		}
		return {
			block: true,
			reason:
				"Deep-refactor blocks pom.xml edits. Fix smells in Java source only. " +
				"Toolchain errors (unsupported source/target, bad JDK) are environment " +
				"issues — stop on this smell; do not change Maven build files.",
		};
	});

	pi.on("agent_start", async () => {
		sendingSmell = false;
	});

	pi.events.on(STOP_EVENT, (data: unknown) => {
		const current = state;
		if (!current || !isDeepRefactorStopEvent(data) || data.caseId !== current.caseId) {
			return;
		}
		externallyStopped = true;
		sendingSmell = false;
		advancingSmell = false;
		persistState({ ...current, active: false });
	});

	pi.on("message_end", async (event, ctx) => {
		const message = event.message;
		if (!message || typeof message !== "object" || !("role" in message)) {
			return;
		}
		if (message.role === "assistant" && state?.active && "usage" in message) {
			const next = recordAssistantTurn(state, message as AssistantMessage);
			persistState(next);
			state = next;
		}
		if (message.role !== "assistant") {
			return;
		}
		if (
			state?.active &&
			"stopReason" in message &&
			shouldAbortAttempt(state.attemptTurnCount, String(message.stopReason))
		) {
			console.warn(
				`[attempt-turn-limit] ${state.caseId}: ${state.currentSmellKey} attempt ${state.attemptNumber}/5 reached ${state.attemptTurnCount} turns`,
			);
			if (ctx.hasUI) {
				ctx.ui.notify(
					`Stopping attempt ${state.attemptNumber}/5 after ${state.attemptTurnCount} LLM turns`,
					"warning",
				);
			}
			ctx.abort();
			return;
		}
		if (!("stopReason" in message) || message.stopReason !== "error") {
			return;
		}
		const detail =
			typeof message.errorMessage === "string" && message.errorMessage
				? message.errorMessage
				: "(assistant stopReason=error, no errorMessage)";
		const caseLabel = state?.caseId ?? "?";
		console.error(`[llm-error] ${caseLabel} assistant: ${detail}`);
		if (ctx.hasUI) {
			ctx.ui.notify(`LLM provider error: ${detail}`, "error");
		}
		if (isRateLimitLlmError(detail)) {
			lastLlmRateLimitDetail = detail;
			return;
		}
		lastLlmRateLimitDetail = null;
		const failureKind = classifyLlmError(detail);
		if (state?.active && failureKind !== null) {
			await reportFatalLlmFailure(state, detail, failureKind, ctx);
		}
	});

	pi.on("agent_settled", async (_event, ctx) => {
		if (!state?.active || sendingSmell) {
			return;
		}
		await handleSettledTurn(ctx, "auto");
	});

	// Docs: https://pi.dev/docs/latest/extensions#pi-on-event-handler
	// Lifecycle: tool_execution_start → tool_call → tool_result (can modify) → tool_execution_end
	pi.on("tool_result", async (event, ctx) => {
		let path = "";
		let toolContent = textFromContent(event.content);
		let bashChangedJava = false;
		if (state?.active && event.toolName === "bash") {
			const fingerprintBefore = javaFingerprintBeforeBash.get(event.toolCallId);
			javaFingerprintBeforeBash.delete(event.toolCallId);
			const fingerprintAfter = await javaTreeFingerprint(pi, state.repoPath);
			bashChangedJava =
				fingerprintBefore !== undefined &&
				fingerprintBefore !== null &&
				fingerprintAfter !== null &&
				fingerprintBefore !== fingerprintAfter;
			if (bashChangedJava) {
				path = "Java files changed by bash";
			}
			if (event.isError && isBashTimeoutError(toolContent)) {
				bashTimeoutStreak += 1;
				const appliedTimeout = applyBashTimeoutPolicy(
					typeof event.input.timeout === "number" ? event.input.timeout : undefined,
					bashTimeoutSec,
					bashTimeoutMaxSec,
				);
				const hint = formatBashTimeoutRecoveryHint({
					streak: bashTimeoutStreak,
					timeoutSec: appliedTimeout,
				});
				if (ctx.hasUI) {
					ctx.ui.notify(
						`Bash timed out (#${bashTimeoutStreak}); inspect the diff and restore named files only.`,
						"warning",
					);
				}
				toolContent = `${toolContent}\n\n${hint}`;
				if (!bashChangedJava) {
					return {
						content: [{ type: "text", text: toolContent }],
						isError: true,
					};
				}
			}
			if (!event.isError) {
				bashTimeoutStreak = 0;
			}
		}
		if (!state?.active || (event.isError && !bashChangedJava)) {
			return undefined;
		}
		if (!bashChangedJava) {
			if (!EDIT_TOOLS.has(event.toolName)) {
				return undefined;
			}
			path = editPathFromEvent(event);
			if (!path || !isCaseJavaEdit(path, ctx.cwd, state.repoPath)) {
				return undefined;
			}
		}
		const editRevision = state.javaEditRevision + 1;
		const expectedSmellKey = state.currentSmellKey;
		const invalidated: DeepCaseState = {
			...state,
			javaEditRevision: editRevision,
			latestVerificationAcceptance: null,
			currentSmellPresent: null,
			latestVerificationDiagnostics: `verification pending for Java edit ${editRevision}`,
		};
		persistState(invalidated);

		if (ctx.hasUI) {
			ctx.ui.setStatus("deep-refactor", `deep: verifying ${path}`);
			ctx.ui.notify(`Automatic verification for ${path}`, "info");
		}
		pi.events.emit(ACTIVITY_EVENT, { phase: "verify", caseId: state.caseId, path });
		const heartbeat = setInterval(() => {
			pi.events.emit(ACTIVITY_EVENT, { phase: "verify", caseId: state?.caseId, path });
		}, activityHeartbeatMs);
		try {
			const result = await runCaseVerification(state, ctx.signal);
			if (externallyStopped || !state?.active) {
				return undefined;
			}
			const updated =
				applyVerificationResult(result, expectedSmellKey, editRevision) ?? state;
			if (updated.latestVerificationAcceptance === true) {
				bashTimeoutStreak = 0;
			}
			const fatalEnvironmentFailure =
				result.fatal_environment_failure ||
				isToolchainVerificationFailure(result.content);
			const report = formatVerificationReport(result, {
				caseId: updated.caseId,
				repoPath: updated.repoPath,
				originalSmellCount: updated.originalSmellCount,
			});
			const combined = `${toolContent}\n\nAutomatic verification:\n${report}`;
			if (ctx.hasUI) {
				ctx.ui.setStatus(
					"deep-refactor",
					result.tests_acceptable
						? statusLabel(updated)
						: `deep: tests failed (${fixedSmellCount(updated.originalSmellCount, updated.remainingSmellCount)}/${updated.originalSmellCount} fixed)`,
				);
			}
			if (fatalEnvironmentFailure) {
				reportFatalEnvironmentFailure(updated, result, ctx);
			}
			return {
				content: [{ type: "text" as const, text: combined }],
				isError: event.isError,
			};
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			if (
				state?.active &&
				state.currentSmellKey === expectedSmellKey &&
				state.javaEditRevision === editRevision
			) {
				persistState({
					...state,
					latestVerificationAcceptance: null,
					currentSmellPresent: null,
					latestVerificationDiagnostics: `verification failed to run: ${message}`,
				});
			}
			if (ctx.hasUI) {
				ctx.ui.notify(`Automatic verification failed: ${message}`, "error");
				ctx.ui.setStatus(
					"deep-refactor",
					`deep: verify error (${state?.caseId ?? "unknown case"})`,
				);
			}
			return {
				content: [
					{
						type: "text" as const,
						text: `${toolContent}\n\nAutomatic verification:\nverification_error=${message}`,
					},
				],
				isError: event.isError,
			};
		} finally {
			clearInterval(heartbeat);
		}
	});

	pi.registerCommand("case", {
		description: "Prepare a dataset case and start deep smell-refactor mode",
		handler: async (args, ctx) => {
			const parsed = parseDeepCaseArgs(args);
			if (!parsed) {
				ctx.ui.notify(
					"Usage: /case <case-id> [profile]  or  /case <case-id> --profile <profile>",
					"warning",
				);
				return;
			}
			const { caseId, profile } = parsed;
			completionReported = false;

			ctx.ui.notify(`Preparing case ${caseId}...`, "info");
			try {
				const prepareArgs = ["prepare", "--case-id", caseId, "--profile", profile];
				if (options.manifest) {
					prepareArgs.push("--manifest", options.manifest);
				}
				const payload = await runPythonJson<PreparePayload>(prepareArgs, {
					timeoutMs: 3 * 60 * 1000,
				});
				if (payload.stop_reason === "smells_cleared" || payload.smell_count === 0) {
					const empty = stateFromPrepare(payload);
					persistState(empty);
					await reportCompletion(empty, "smells_cleared", ctx);
					return;
				}

				const firstTask =
					(Array.isArray(payload.tasks) && payload.tasks[0]) || payload.task || "";
				const nextState = stateFromPrepare(payload);
				persistState(nextState);
				pi.setSessionName(`deep:${payload.case_id}`);
				ctx.ui.setStatus("deep-refactor", statusLabel(nextState));
				ctx.ui.notify(
					`Deep case ready: ${payload.smell_count} smells (one-by-one) in ${payload.repo_path}`,
					"info",
				);

				if (!ctx.isIdle()) {
					ctx.ui.notify(
						"Agent is busy; first smell was not sent. Run /next when idle.",
						"warning",
					);
					return;
				}
				if (!firstTask) {
					ctx.ui.notify("Prepare returned no smell task", "error");
					return;
				}
				if (!payload.current_smell_key) {
					ctx.ui.notify("Prepare returned no current smell key", "error");
					return;
				}
				await sendNewSmell(
					firstTask,
					payload.current_smell_key,
					ctx,
					payload.smell_count,
				);
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				ctx.ui.notify(`case failed: ${message}`, "error");
			}
		},
	});

	pi.registerCommand("next", {
		description: "Apply the verification gate, then retry, skip, or advance",
		handler: async (_args, ctx) => {
			if (!state?.active) {
				ctx.ui.notify("No active refactor case. Run /case first.", "warning");
				return;
			}
			if (!ctx.isIdle()) {
				ctx.ui.notify("Agent is busy; wait until idle, then /next again.", "warning");
				return;
			}
			if (!state.currentSmellTask) {
				await sendNextSmell(ctx, { source: "manual" });
				return;
			}
			await handleSettledTurn(ctx, "manual");
		},
	});

	pi.registerCommand("verify", {
		description: "Run Maven + ORGANIC verification for the active deep case",
		handler: async (_args, ctx) => {
			if (!state?.active) {
				ctx.ui.notify("No active refactor case. Run /case first.", "warning");
				return;
			}

			ctx.ui.setStatus("deep-refactor", `deep: verifying ${state.caseId}`);
			try {
				const expectedSmellKey = state.currentSmellKey;
				const editRevision = state.javaEditRevision;
				const result = await runCaseVerification(state);
				const updated =
					applyVerificationResult(result, expectedSmellKey, editRevision) ?? state;
				const report = formatVerificationReport(result, {
					caseId: state.caseId,
					repoPath: state.repoPath,
					originalSmellCount: updated.originalSmellCount,
				});
				pi.sendMessage({
					customType: VERIFY_MESSAGE_TYPE,
					content: report,
					display: true,
					details: {
						passed: result.passed,
						tests_acceptable: result.tests_acceptable,
						test_status: result.test_status,
						flaky_tests: result.flaky_tests,
						current_smell_present: result.current_smell_present,
						caseId: state.caseId,
						repoPath: state.repoPath,
						raw: result.content,
						remaining_smells: updated.remainingSmellCount,
						fixed_smells: updated.fixedSmellCount,
						introduced_smells: updated.introducedSmellCount,
						fixed_introduced_smells: updated.fixedIntroducedSmellCount,
						total_introduced_smells: updated.totalIntroducedSmellCount,
						original_smells: updated.originalSmellCount,
					},
				});
				ctx.ui.setStatus(
					"deep-refactor",
					result.tests_acceptable
						? statusLabel(updated)
						: `deep: tests failed (${fixedSmellCount(updated.originalSmellCount, updated.remainingSmellCount)}/${updated.originalSmellCount} fixed)`,
				);
				if (
					result.fatal_environment_failure ||
					isToolchainVerificationFailure(result.content)
				) {
					reportFatalEnvironmentFailure(updated, result, ctx);
				}
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				if (state?.active) {
					persistState({
						...state,
						latestVerificationAcceptance: null,
						currentSmellPresent: null,
						latestVerificationDiagnostics: `verification failed to run: ${message}`,
					});
				}
				const report = [
					"Automatic verification — ERROR",
					`case: ${state.caseId}`,
					`repo: ${state.repoPath}`,
					`error: ${message}`,
				].join("\n");
				pi.sendMessage({
					customType: VERIFY_MESSAGE_TYPE,
					content: report,
					display: true,
					details: { passed: false, caseId: state.caseId, repoPath: state.repoPath },
				});
				ctx.ui.setStatus("deep-refactor", `deep: verify error (${state.caseId})`);
			}
		},
	});

	pi.registerCommand("status", {
		description: "Show the active deep-refactor case",
		handler: async (_args, ctx) => {
			if (!state?.active) {
				ctx.ui.notify("No active refactor case", "info");
				return;
			}
			const fixed = state.fixedSmellCount;
			const introducedRemaining = state.introducedSmellCount;
			ctx.ui.notify(
				[
					`case_id=${state.caseId}`,
					`project=${state.project}`,
					`repo_path=${state.repoPath}`,
					`profile=${state.profile}`,
					`timeout=${state.timeout}`,
					`elements=${state.elements.length}`,
					`original_smells=${state.originalSmellCount}`,
					`remaining_smells=${state.remainingSmellCount}`,
					`fixed_smells=${fixed}`,
					`introduced_smells_remaining=${introducedRemaining}`,
					`fixed_introduced_smells=${state.fixedIntroducedSmellCount}`,
					`total_introduced_smells=${state.totalIntroducedSmellCount}`,
					`sent_count=${state.sentCount}`,
					`current_smell_key=${state.currentSmellKey || "none"}`,
					`attempt=${state.attemptNumber}/5`,
					`tests_acceptable=${state.latestVerificationAcceptance ?? "unknown"}`,
					`current_smell_present=${state.currentSmellPresent ?? "unknown"}`,
					`latest_edit_revision=${state.javaEditRevision}`,
					`verified_edit_revision=${state.verifiedJavaEditRevision}`,
					`gate_satisfied=${verificationGateSatisfied(state)}`,
					`skipped_smell_keys=${JSON.stringify(state.skippedSmellKeys)}`,
					`verification=${state.latestVerificationDiagnostics}`,
					"commands: /next /verify /stop",
				].join("\n"),
				"info",
			);
		},
	});

	pi.registerCommand("stop", {
		description: "Disable automatic Java verification hooks",
		handler: async (_args, ctx) => {
			if (state?.active && !completionReported) {
				await reportCompletion(state, "stopped", ctx);
			}
			persistState(null);
			sendingSmell = false;
			ctx.ui.setStatus("deep-refactor", undefined);
			ctx.ui.notify("Refactor mode stopped", "info");
		},
	});
}
