import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { loadOpenCodeApiKey, loadPilotConfig } from "./config.js";
import {
  createDeepSeekModel,
  createDeepSeekStreamFn,
} from "./agents/pi-runtime.js";
import { preflightPilot, runPilot } from "./pilot-runner.js";

interface CliOptions {
  db: string;
  config: string;
  output: string;
  globalIndexes: number[];
  preflightOnly: boolean;
  openCodeAuth?: string;
}

function parseIndexes(value: string): number[] {
  const result: number[] = [];
  for (const part of value.split(",")) {
    const match = /^(\d+)(?:-(\d+))?$/u.exec(part.trim());
    if (match === null) {
      throw new Error(`invalid --global-index value: ${value}`);
    }
    const start = Number(match[1]);
    const end = Number(match[2] ?? match[1]);
    if (end < start || end - start > 10_000) {
      throw new Error(`invalid --global-index range: ${part}`);
    }
    for (let index = start; index <= end; index += 1) {
      result.push(index);
    }
  }
  return [...new Set(result)].sort((left, right) => left - right);
}

function parseArgs(argv: readonly string[]): CliOptions {
  if (argv[0] !== "preview") {
    throw new Error("usage: pilot preview --db PATH --config PATH --global-index 219-223 --output PATH [--preflight-only]");
  }
  const values = new Map<string, string>();
  let preflightOnly = false;
  for (let index = 1; index < argv.length; index += 1) {
    const argument = argv[index] as string;
    if (argument === "--preflight-only") {
      preflightOnly = true;
      continue;
    }
    if (!argument.startsWith("--")) {
      throw new Error(`unexpected argument: ${argument}`);
    }
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) {
      throw new Error(`missing value for ${argument}`);
    }
    values.set(argument, value);
    index += 1;
  }
  const requireValue = (name: string): string => {
    const value = values.get(name);
    if (value === undefined) {
      throw new Error(`missing ${name}`);
    }
    return resolve(value);
  };
  return {
    db: requireValue("--db"),
    config: requireValue("--config"),
    output: requireValue("--output"),
    globalIndexes: parseIndexes(values.get("--global-index") ?? ""),
    preflightOnly,
    ...(values.get("--opencode-auth") === undefined
      ? {}
      : { openCodeAuth: resolve(values.get("--opencode-auth") as string) }),
  };
}

export async function main(argv = process.argv.slice(2)): Promise<void> {
  const options = parseArgs(argv);
  const baseConfig = loadPilotConfig(options.config, "draft");
  const config = options.openCodeAuth === undefined
    ? baseConfig
    : loadPilotConfig(options.config, "draft", {
      apiKeyOverride: loadOpenCodeApiKey(
        options.openCodeAuth,
        baseConfig.provider,
      ),
    });
  const preflight = preflightPilot(options.db, options.globalIndexes);
  console.log(JSON.stringify({
    mode: "cold-preview",
    ...preflight,
    model: config.toJSON(),
  }, null, 2));
  if (options.preflightOnly) {
    return;
  }
  const model = createDeepSeekModel(config);
  const result = await runPilot({
    dbPath: options.db,
    outputDir: options.output,
    globalIndexes: options.globalIndexes,
    model,
    streamFn: createDeepSeekStreamFn(config),
  });
  console.log(JSON.stringify({
    status: result.status,
    translations: result.translations.length,
    metrics: result.metrics,
    artifacts: result.artifacts,
  }, null, 2));
}

const entry = process.argv[1] === undefined
  ? undefined
  : pathToFileURL(resolve(process.argv[1])).href;
if (entry === import.meta.url) {
  main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
