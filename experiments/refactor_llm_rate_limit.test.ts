import { expect, test } from "bun:test";
import {
	MAX_LLM_TURNS_PER_ATTEMPT,
	STRONG_MODEL,
	WEAK_MODEL,
	classifyLlmError,
	isRateLimitLlmError,
	modelForAttempt,
	rateLimitRetryDelayMs,
	shouldAbortAttempt,
} from "../agents/pi/refactor.ts";

const upstream429 =
	'429: {"message":"Provider returned error","code":429,"metadata":{"raw":"qwen/qwen3.7-flash is temporarily rate-limited upstream","provider_error_code":"insufficient_quota","limit_source":"upstream_provider_shared_pool"}}';

test("rate-limit detector matches OpenRouter upstream 429 text", () => {
	expect(isRateLimitLlmError(upstream429)).toBeTrue();
	expect(isRateLimitLlmError("Too Many Requests")).toBeTrue();
	expect(isRateLimitLlmError("rate_limit_exceeded")).toBeTrue();
	expect(isRateLimitLlmError("401 invalid api key")).toBeFalse();
	expect(isRateLimitLlmError("model not found")).toBeFalse();
});

test("rate-limit errors are not fatal auth/provider failures", () => {
	expect(classifyLlmError(upstream429)).toBeNull();
	expect(classifyLlmError("429 Too Many Requests")).toBeNull();
	expect(classifyLlmError("401 invalid api key")).toBe("auth");
	expect(classifyLlmError("model not found")).toBe("provider");
});

test("rate-limit delay prefers Retry-After and caps backoff", () => {
	expect(rateLimitRetryDelayMs("Retry-After: 12", 1)).toBe(12_000);
	expect(rateLimitRetryDelayMs('{"retry_after": 8}', 3)).toBe(8_000);
	const original = Math.random;
	Math.random = () => 0;
	try {
		expect(rateLimitRetryDelayMs("429 rate limited", 1)).toBe(5_000);
		expect(rateLimitRetryDelayMs("429 rate limited", 2)).toBe(10_000);
		expect(rateLimitRetryDelayMs("429 rate limited", 10)).toBe(120_000);
	} finally {
		Math.random = original;
	}
});

test("the stronger model starts on the third real attempt", () => {
	expect(modelForAttempt(1)).toEqual(WEAK_MODEL);
	expect(modelForAttempt(2)).toEqual(WEAK_MODEL);
	expect(modelForAttempt(3)).toEqual(STRONG_MODEL);
	expect(modelForAttempt(5)).toEqual(STRONG_MODEL);
	expect(WEAK_MODEL.id).toBe("deepseek/deepseek-v4-flash-0731");
	expect(STRONG_MODEL.id).toBe("upstage/solar-pro4");
});

test("tool-use attempts stop at the per-attempt turn limit", () => {
	expect(shouldAbortAttempt(MAX_LLM_TURNS_PER_ATTEMPT - 1, "toolUse")).toBeFalse();
	expect(shouldAbortAttempt(MAX_LLM_TURNS_PER_ATTEMPT, "toolUse")).toBeTrue();
	expect(shouldAbortAttempt(MAX_LLM_TURNS_PER_ATTEMPT, "stop")).toBeFalse();
	expect(shouldAbortAttempt(MAX_LLM_TURNS_PER_ATTEMPT, "error")).toBeFalse();
});
