/**
 * Token and cost accounting for the pi deep-refactor harness.
 */

import type { Usage } from "@earendil-works/pi-ai";

export type UsageTotals = {
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	cost: number;
	turnCount: number;
};

export function createUsageTotals(): UsageTotals {
	return {
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		cost: 0,
		turnCount: 0,
	};
}

export function addUsageToTotals(totals: UsageTotals, usage: Usage): UsageTotals {
	return {
		input: totals.input + usage.input,
		output: totals.output + usage.output,
		cacheRead: totals.cacheRead + usage.cacheRead,
		cacheWrite: totals.cacheWrite + usage.cacheWrite,
		cost: totals.cost + usage.cost.total,
		turnCount: totals.turnCount + 1,
	};
}

export function usageTotalsFields(totals: UsageTotals): Record<string, number> {
	return {
		input_tokens: totals.input,
		output_tokens: totals.output,
		cache_read_tokens: totals.cacheRead,
		cache_write_tokens: totals.cacheWrite,
		total_tokens: totals.input + totals.output,
		cost_usd: roundCost(totals.cost),
		turn_count: totals.turnCount,
	};
}

export function usageTurnFields(usage: Usage): Record<string, number> {
	return {
		input_tokens: usage.input,
		output_tokens: usage.output,
		cache_read_tokens: usage.cacheRead,
		cache_write_tokens: usage.cacheWrite,
		total_tokens: usage.totalTokens,
		cost_input_usd: roundCost(usage.cost.input),
		cost_output_usd: roundCost(usage.cost.output),
		cost_total_usd: roundCost(usage.cost.total),
	};
}

export function roundCost(value: number): number {
	return Math.round(value * 1_000_000) / 1_000_000;
}
