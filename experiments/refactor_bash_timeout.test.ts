import { expect, test } from "bun:test";
import {
	DEFAULT_BASH_TIMEOUT_SEC,
	MAX_BASH_TIMEOUT_SEC,
	applyBashTimeoutPolicy,
	formatBashTimeoutRecoveryHint,
	isBroadGitRestoreCommand,
	isBashTimeoutError,
} from "../agents/pi/refactor.ts";

test("bash timeout policy fills missing values and caps highs", () => {
	expect(applyBashTimeoutPolicy(undefined)).toBe(DEFAULT_BASH_TIMEOUT_SEC);
	expect(applyBashTimeoutPolicy(0)).toBe(DEFAULT_BASH_TIMEOUT_SEC);
	expect(applyBashTimeoutPolicy(-5)).toBe(DEFAULT_BASH_TIMEOUT_SEC);
	expect(applyBashTimeoutPolicy(60)).toBe(60);
	expect(applyBashTimeoutPolicy(9999)).toBe(MAX_BASH_TIMEOUT_SEC);
	expect(applyBashTimeoutPolicy(undefined, 120, 240)).toBe(120);
	expect(applyBashTimeoutPolicy(500, 120, 240)).toBe(240);
});

test("bash timeout error detector matches SDK message", () => {
	expect(isBashTimeoutError("Command timed out after 180 seconds")).toBeTrue();
	expect(isBashTimeoutError("exit 1\nCommand timed out after 12 seconds\n")).toBeTrue();
	expect(isBashTimeoutError("mvn failed with exit code 1")).toBeFalse();
});

test("bash timeout recovery hint keeps the case retryable and limits git restore", () => {
	const first = formatBashTimeoutRecoveryHint({ streak: 1, timeoutSec: 180 });
	expect(first).toContain("case is still active");
	expect(first).toContain("restore only the named files");
	expect(first).toContain("Never restore the whole worktree");
	expect(first).toContain("automatic verification");
	expect(first).not.toContain("Repeated bash timeouts");

	const later = formatBashTimeoutRecoveryHint({ streak: 3, timeoutSec: 180 });
	expect(later).toContain("consecutive timeout #3");
	expect(later).toContain("Repeated bash timeouts");
	expect(later).toContain("Avoid `./gradlew test`");
});

test("whole-worktree restore is blocked while named-file restore remains allowed", () => {
	expect(isBroadGitRestoreCommand("git checkout HEAD -- .")).toBeTrue();
	expect(isBroadGitRestoreCommand("git checkout -- . && git status")).toBeTrue();
	expect(isBroadGitRestoreCommand("git restore --source=HEAD --worktree -- .")).toBeTrue();
	expect(isBroadGitRestoreCommand("git restore .")).toBeTrue();
	expect(isBroadGitRestoreCommand("git restore src/main/java/Foo.java")).toBeFalse();
	expect(isBroadGitRestoreCommand("git checkout HEAD -- src/main/java/Foo.java")).toBeFalse();
});
