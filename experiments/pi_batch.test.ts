import { expect, mock, test } from "bun:test";
import { createEventBus } from "@earendil-works/pi-coding-agent";
import { COMPLETE_EVENT, type DeepRefactorCompleteEvent } from "../agents/pi/refactor.ts";
import {
	activityHeartbeatInterval,
	buildManifestResume,
	createCaseWaiter,
	isCaseOk,
	isExperimentCompleted,
	remainingTimeoutDelay,
	withUsageJsonArg,
} from "./pi_batch.ts";

const completionEvent: DeepRefactorCompleteEvent = {
	caseId: "case",
	repoPath: "/repo",
	stopReason: "smells_cleared",
	originalSmellCount: 1,
	remainingSmellCount: 0,
	fixedSmellCount: 1,
	introducedSmellCount: 0,
	fixedIntroducedSmellCount: 0,
	totalIntroducedSmellCount: 0,
	profile: "without-planning",
	ck: null,
	ckError: null,
	usageJson: JSON.stringify({
		input_tokens: 1,
		output_tokens: 2,
		cache_read_tokens: 0,
		cache_write_tokens: 0,
		total_tokens: 3,
		cost_usd: 0,
		turn_count: 1,
	}),
};

test("active hooks report activity before a short idle deadline", () => {
	expect(activityHeartbeatInterval(50)).toBe(25);
	expect(activityHeartbeatInterval(1)).toBe(1);
});

test("premature timer callbacks retain the unelapsed timeout", () => {
	const fourHoursMs = 4 * 60 * 60 * 1000;
	const elapsedMs = 36 * 60 * 1000;

	expect(remainingTimeoutDelay(1_000, fourHoursMs, 1_000 + elapsedMs)).toBe(
		fourHoursMs - elapsedMs,
	);
	expect(remainingTimeoutDelay(1_000, fourHoursMs, 1_000 + fourHoursMs)).toBe(0);
});

test("smells cleared still requires zero remaining and introduced smells", () => {
	expect(isExperimentCompleted("smells_cleared", 1, 0)).toBeFalse();
	expect(isExperimentCompleted("smells_cleared", 0, 1)).toBeFalse();
	expect(isExperimentCompleted("smells_cleared", 0, 0)).toBeTrue();
	expect(isExperimentCompleted("smells_exhausted", 1, 0)).toBeFalse();
	expect(isExperimentCompleted("smells_exhausted", 0, 1)).toBeFalse();
	expect(isExperimentCompleted("smells_exhausted", 0, 0)).toBeTrue();
});

test("case ok requires completion ck_error null, not the batch terminal event field", () => {
	expect(isCaseOk("smells_cleared", 0, 0, null)).toBeTrue();
	expect(isCaseOk("smells_cleared", 0, 0, "ck failed")).toBeFalse();
	expect(isCaseOk("smells_cleared", 1, 0, null)).toBeFalse();
});

test("batch complete args forward usage-json from the terminal event", () => {
	const args = withUsageJsonArg(
		["complete", "--case-id", "case"],
		completionEvent.usageJson,
	);
	expect(args).toContain("--usage-json");
	expect(args.at(-1)).toBe(completionEvent.usageJson);
	expect(withUsageJsonArg(["complete"], null)).toEqual(["complete"]);
});

test("wall timeout accepts an in-flight terminal event during its grace period", async () => {
	const eventBus = createEventBus();
	const onTimeout = mock(() => undefined);
	const waiter = createCaseWaiter(eventBus, "case", 25, onTimeout, 25);

	await new Promise((resolve) => setTimeout(resolve, 30));
	eventBus.emit(COMPLETE_EVENT, completionEvent);

	await expect(waiter.promise).resolves.toEqual(completionEvent);
	await new Promise((resolve) => setTimeout(resolve, 30));
	expect(onTimeout).not.toHaveBeenCalled();
});

test("wall timeout settles once after grace when no terminal event arrives", async () => {
	const eventBus = createEventBus();
	const onTimeout = mock(() => undefined);
	const waiter = createCaseWaiter(eventBus, "case", 25, onTimeout, 25);
	const rejection = expect(waiter.promise).rejects.toThrow("Timed out waiting");

	await new Promise((resolve) => setTimeout(resolve, 60));

	await rejection;
	expect(onTimeout).toHaveBeenCalledTimes(1);
});

test("manifest resume marks completed and pending cases", () => {
	const manifestCases = [
		{ case_id: "Drugis Common:bcbdecd601d0", project: "Drugis Common" },
		{ case_id: "Drugis Common:6cd5f088448e", project: "Drugis Common" },
		{ case_id: "Tap4j:462ab4c8c308", project: "Tap4j" },
	];
	const results = [
		{
			caseId: "Drugis Common:bcbdecd601d0",
			project: "Drugis Common",
			ok: true,
			stopReason: "smells_cleared",
			fixedSmellCount: 17,
			introducedSmellCount: 0,
			fixedIntroducedSmellCount: 16,
			totalIntroducedSmellCount: 16,
			originalSmellCount: 17,
			remainingSmellCount: 0,
			ckError: null,
			error: null,
			repoPath: "/repo/a",
		},
		{
			caseId: "Drugis Common:6cd5f088448e",
			project: "Drugis Common",
			ok: false,
			stopReason: "smells_exhausted",
			fixedSmellCount: 19,
			introducedSmellCount: 0,
			fixedIntroducedSmellCount: 11,
			totalIntroducedSmellCount: 11,
			originalSmellCount: 20,
			remainingSmellCount: 4,
			ckError: null,
			error: "smells_exhausted",
			repoPath: "/repo/b",
		},
	];

	const resume = buildManifestResume(manifestCases, results);

	expect(resume.completed_count).toBe(2);
	expect(resume.pending_count).toBe(1);
	expect(resume.successful_count).toBe(1);
	expect(resume.failed_count).toBe(1);
	expect(resume.cases[0]?.status).toBe("successful");
	expect(resume.cases[0]?.stop_reason).toBe("smells_cleared");
	expect(resume.cases[1]?.status).toBe("failed");
	expect(resume.cases[2]?.status).toBe("pending");
});
