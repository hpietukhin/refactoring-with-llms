/** Load project and local mise ``[env]`` values for pi batch runs. */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

function parseTomlString(raw: string): string {
	if (
		(raw.startsWith('"') && raw.endsWith('"')) ||
		(raw.startsWith("'") && raw.endsWith("'"))
	) {
		return raw.slice(1, -1);
	}
	return raw;
}

export function parseMiseEnvSection(miseTomlPath: string): Record<string, string> {
	if (!existsSync(miseTomlPath)) {
		return {};
	}
	const result: Record<string, string> = {};
	let inEnv = false;
	for (const line of readFileSync(miseTomlPath, "utf8").split("\n")) {
		const trimmed = line.trim();
		if (!trimmed || trimmed.startsWith("#")) {
			continue;
		}
		if (trimmed === "[env]") {
			inEnv = true;
			continue;
		}
		if (trimmed.startsWith("[")) {
			inEnv = false;
			continue;
		}
		if (!inEnv) {
			continue;
		}
		const match = trimmed.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$/);
		if (!match) {
			continue;
		}
		result[match[1]] = parseTomlString(match[2].trim());
	}
	return result;
}

/** Apply tracked defaults, then credentials from ignored ``mise.local.toml``. */
export function applyMiseTomlEnv(repoRoot: string): void {
	const env = {
		...parseMiseEnvSection(join(repoRoot, "mise.toml")),
		...parseMiseEnvSection(join(repoRoot, "mise.local.toml")),
	};
	for (const [key, value] of Object.entries(env)) {
		if (value) {
			process.env[key] = value;
		}
	}
}
