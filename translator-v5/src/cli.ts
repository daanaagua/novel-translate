import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import {
  createDeepSeekModel,
  createDeepSeekStreamFn,
} from "./agents/pi-runtime.js";
import { loadOpenCodeApiKey, loadPilotConfig } from "./config.js";
import { preflightBook, runBook } from "./fullbook/book-runner.js";
import { preflightPilot, runPilot } from "./pilot-runner.js";
import { writeBookArtifacts } from "./report.js";
import { BookStore } from "./storage/book-store.js";

export type CliCommand =
  | "preview"
  | "book-preflight"
  | "book-run"
  | "book-status"
  | "book-export";

export interface CliOptions {
  command: CliCommand;
  db?: string;
  config?: string;
  output?: string;
  store?: string;
  globalIndexes?: number[];
  preflightOnly?: boolean;
  allowIncomplete?: boolean;
  openCodeAuth?: string;
  maxWindows?: number;
  maxConcurrency?: number;
  maxAttempts?: number;
  maxBlocks?: number;
  maxSourceTokens?: number;
  hardDeadlineMs?: number;
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

function positiveFlag(
  values: ReadonlyMap<string, string>,
  name: string,
): number | undefined {
  const raw = values.get(name);
  if (raw === undefined) {
    return undefined;
  }
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

function parseFlags(argv: readonly string[]): {
  values: Map<string, string>;
  booleans: Set<string>;
} {
  const values = new Map<string, string>();
  const booleans = new Set<string>();
  const booleanNames = new Set(["--preflight-only", "--allow-incomplete"]);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index] as string;
    if (booleanNames.has(argument)) {
      booleans.add(argument);
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
  return { values, booleans };
}

function pathValue(
  values: ReadonlyMap<string, string>,
  name: string,
  required = true,
): string | undefined {
  const value = values.get(name);
  if (value === undefined) {
    if (required) {
      throw new Error(`missing ${name}`);
    }
    return undefined;
  }
  return resolve(value);
}

export function parseArgs(argv: readonly string[]): CliOptions {
  if (argv[0] === "preview") {
    const { values, booleans } = parseFlags(argv.slice(1));
    return {
      command: "preview",
      db: pathValue(values, "--db"),
      config: pathValue(values, "--config"),
      output: pathValue(values, "--output"),
      globalIndexes: parseIndexes(values.get("--global-index") ?? ""),
      preflightOnly: booleans.has("--preflight-only"),
      ...(pathValue(values, "--opencode-auth", false) === undefined
        ? {}
        : { openCodeAuth: pathValue(values, "--opencode-auth", false) }),
    };
  }
  if (argv[0] !== "book" || argv[1] === undefined) {
    throw new Error("usage: pilot preview ... | pilot book preflight|run|status|export ...");
  }
  const action = argv[1];
  const { values, booleans } = parseFlags(argv.slice(2));
  const common = {
    maxBlocks: positiveFlag(values, "--max-blocks"),
    maxSourceTokens: positiveFlag(values, "--max-source-tokens"),
  };
  if (action === "preflight") {
    return {
      command: "book-preflight",
      db: pathValue(values, "--db"),
      ...common,
    };
  }
  if (action === "run") {
    return {
      command: "book-run",
      db: pathValue(values, "--db"),
      store: pathValue(values, "--store"),
      config: pathValue(values, "--config"),
      output: pathValue(values, "--output"),
      openCodeAuth: pathValue(values, "--opencode-auth", false),
      maxWindows: positiveFlag(values, "--max-windows"),
      maxConcurrency: positiveFlag(values, "--max-concurrency"),
      maxAttempts: positiveFlag(values, "--max-attempts"),
      hardDeadlineMs: positiveFlag(values, "--hard-deadline-ms"),
      ...common,
    };
  }
  if (action === "status") {
    return { command: "book-status", store: pathValue(values, "--store") };
  }
  if (action === "export") {
    return {
      command: "book-export",
      store: pathValue(values, "--store"),
      output: pathValue(values, "--output"),
      allowIncomplete: booleans.has("--allow-incomplete"),
    };
  }
  throw new Error(`unknown book action: ${action}`);
}

function requireOption(options: CliOptions, name: keyof CliOptions): string {
  const value = options[name];
  if (typeof value !== "string") {
    throw new Error(`missing ${String(name)}`);
  }
  return value;
}

function loadRuntimeConfig(options: CliOptions) {
  const baseConfig = loadPilotConfig(requireOption(options, "config"), "draft");
  return options.openCodeAuth === undefined
    ? baseConfig
    : loadPilotConfig(requireOption(options, "config"), "draft", {
      apiKeyOverride: loadOpenCodeApiKey(options.openCodeAuth, baseConfig.provider),
    });
}

export async function main(argv = process.argv.slice(2)): Promise<void> {
  const options = parseArgs(argv);
  if (options.command === "book-preflight") {
    console.log(JSON.stringify(preflightBook(requireOption(options, "db"), {
      maxBlocks: options.maxBlocks,
      maxSourceTokens: options.maxSourceTokens,
    }), null, 2));
    return;
  }
  if (options.command === "book-status") {
    const store = new BookStore(requireOption(options, "store"));
    try {
      console.log(JSON.stringify({
        status: store.statusSummary(),
        windows: store.allWindows(),
      }, null, 2));
    } finally {
      store.close();
    }
    return;
  }
  if (options.command === "book-export") {
    const store = new BookStore(requireOption(options, "store"));
    try {
      console.log(JSON.stringify(writeBookArtifacts(
        store,
        requireOption(options, "output"),
        { allowIncomplete: options.allowIncomplete },
      ), null, 2));
    } finally {
      store.close();
    }
    return;
  }

  const config = loadRuntimeConfig(options);
  const model = createDeepSeekModel(config);
  const streamFn = createDeepSeekStreamFn(config);
  if (options.command === "book-run") {
    const result = await runBook({
      dbPath: requireOption(options, "db"),
      storePath: requireOption(options, "store"),
      outputDir: requireOption(options, "output"),
      model,
      streamFn,
      windowOptions: {
        maxBlocks: options.maxBlocks,
        maxSourceTokens: options.maxSourceTokens,
      },
      maxWindows: options.maxWindows,
      maxConcurrency: options.maxConcurrency,
      maxAttempts: options.maxAttempts,
      hardDeadlineMs: options.hardDeadlineMs,
    });
    console.log(JSON.stringify(result, null, 2));
    return;
  }

  const db = requireOption(options, "db");
  const indexes = options.globalIndexes ?? [];
  const preflight = preflightPilot(db, indexes);
  console.log(JSON.stringify({
    mode: "cold-preview",
    ...preflight,
    model: config.toJSON(),
  }, null, 2));
  if (options.preflightOnly) {
    return;
  }
  const result = await runPilot({
    dbPath: db,
    outputDir: requireOption(options, "output"),
    globalIndexes: indexes,
    model,
    streamFn,
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
