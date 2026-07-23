import { basename, extname } from "node:path";

import type {
  DesktopChooseSourceResult,
  DesktopConfirmSourceEncodingRequest,
  DesktopDoctorReport,
  DesktopModelOption,
  DesktopModelProbe,
  DesktopModelSummary,
  DesktopOnboardingProvider,
  DesktopOnboardingState,
  DesktopProjectRequest,
  DesktopProjectSnapshot,
  DesktopResult,
  DesktopStartTrialRequest,
  DesktopTestModelResult,
  DesktopTrialMode,
  DesktopTrialResult,
} from "../contracts.js";
import type {
  DesktopDiscoverModelsRequest as ServiceDiscoverModelsRequest,
  DesktopTestModelRequest as ServiceTestModelRequest,
} from "../desktop-model-service.js";
import type {
  DesktopConfirmEncodingRequest,
  DesktopSourceEncodingRequiredResult,
  DesktopSourceImportRequest,
  DesktopSourceReadyResult,
} from "../desktop-source-service.js";
import type { ProviderEffort, ProviderId } from "../../providers/types.js";
import { DesktopInputError, fail, ok, toDesktopError } from "../desktop-errors.js";

export const DESKTOP_IPC_CHANNELS = [
  "folioloom:choose-source",
  "folioloom:confirm-source-encoding",
  "folioloom:onboarding-state",
  "folioloom:discover-models",
  "folioloom:test-model",
  "folioloom:forget-credential",
  "folioloom:start-trial",
  "folioloom:cancel-trial",
  // Legacy project controls remain for existing developer workspaces.
  "folioloom:choose-project",
  "folioloom:choose-store",
  "folioloom:refresh-project",
  "folioloom:select-run",
  "folioloom:doctor",
] as const;

export type DesktopIpcChannel = (typeof DESKTOP_IPC_CHANNELS)[number];
export type DesktopIpcHandler = (event: unknown, ...args: unknown[]) => Promise<unknown>;

export interface DesktopIpcMain {
  handle(channel: DesktopIpcChannel, handler: DesktopIpcHandler): void;
}

export interface DesktopOpenDialogOptions {
  properties: "openFile"[];
  filters: Array<{ name: string; extensions: string[] }>;
}

export interface DesktopDialog {
  showOpenDialog(options: DesktopOpenDialogOptions): Promise<{
    canceled: boolean;
    filePaths: string[];
  }>;
}

export interface DesktopIpcProjectService {
  snapshot(request: DesktopProjectRequest): DesktopResult<DesktopProjectSnapshot>;
  doctor(
    request: Pick<DesktopProjectRequest, "manifestPath" | "glossaryPath">,
  ): DesktopResult<DesktopDoctorReport>;
}

export interface DesktopIpcSourceService {
  importSource(
    request: Pick<DesktopSourceImportRequest, "sourcePath" | "sourceLanguage" | "explicitEncoding">,
  ): Promise<DesktopSourceEncodingRequiredResult | Pick<DesktopSourceReadyResult, "status" | "manifestPath">>;
  confirmEncoding(
    request: DesktopConfirmEncodingRequest,
  ): Promise<Pick<DesktopSourceReadyResult, "status" | "manifestPath">>;
}

export interface DesktopIpcModelSnapshot {
  providers: readonly DesktopOnboardingProvider[];
  activeModelProfile?: {
    providerId: string;
    modelId: string;
    reasoningEffort?: string;
    customBaseUrl?: string;
  };
  latestProbe?: DesktopModelProbe;
}

export interface DesktopIpcModelTestResult {
  report: DesktopModelProbe;
  snapshot: DesktopIpcModelSnapshot;
}

export interface DesktopIpcModelService {
  snapshot(): DesktopIpcModelSnapshot;
  discoverModels(request: ServiceDiscoverModelsRequest): Promise<readonly { id: string; displayName: string }[]>;
  testAndSave(request: ServiceTestModelRequest): Promise<DesktopIpcModelTestResult>;
  forgetCredential(providerId: ProviderId): void;
}

export interface DesktopIpcTrialService {
  start(request: { manifestPath: string; mode: DesktopTrialMode }): Promise<DesktopTrialResult>;
  cancel(): Promise<void>;
}

export interface DesktopIpcDependencies {
  ipcMain: DesktopIpcMain;
  dialog: DesktopDialog;
  projectService: DesktopIpcProjectService;
  sourceService: DesktopIpcSourceService;
  modelService: DesktopIpcModelService;
  trialService: DesktopIpcTrialService;
  isTrustedEvent(event: unknown): boolean;
  getCurrentRequest(): DesktopProjectRequest | undefined;
  setCurrentRequest(request: DesktopProjectRequest): void;
}

const manuscriptFilter = [{ name: "书稿", extensions: ["txt", "md", "markdown", "epub", "docx"] }];
const manifestFilter = [{ name: "FolioLoom 项目", extensions: ["json"] }];
const storeFilter = [{ name: "FolioLoom 状态库", extensions: ["db"] }];
const PROVIDER_ID = /^[a-z][a-z0-9-]{0,63}$/u;
const SOURCE_ENCODINGS = new Set([
  "utf-8", "utf-16le", "utf-16be", "utf-32le", "utf-32be",
  "shift_jis", "euc-jp", "euc-kr", "windows-949",
]);

function failure<T = never>(code: string, message: string): DesktopResult<T> {
  return fail({ code, message, retryable: false });
}

function noOpenProject(): DesktopResult<DesktopProjectSnapshot> {
  return failure("DESKTOP_NO_PROJECT", "open an initialized project first");
}

function canceledSelection(): DesktopResult<never> {
  return failure("DESKTOP_SELECTION_CANCELLED", "no file was selected");
}

function invalidSelection(message: string): DesktopResult<never> {
  return failure("DESKTOP_INPUT_INVALID", message);
}

function untrustedEvent(): DesktopResult<never> {
  return failure("DESKTOP_UNTRUSTED_IPC", "IPC caller is not the trusted FolioLoom renderer");
}

async function resultFrom<T>(
  operation: () => DesktopResult<T> | Promise<DesktopResult<T>>,
): Promise<DesktopResult<T>> {
  try {
    return await operation();
  } catch (error) {
    return fail(toDesktopError(error));
  }
}

async function chooseSingleFile(
  dialog: DesktopDialog,
  filters: Array<{ name: string; extensions: string[] }>,
): Promise<string | undefined> {
  const selection = await dialog.showOpenDialog({ properties: ["openFile"], filters });
  return selection.canceled || selection.filePaths.length !== 1
    ? undefined
    : selection.filePaths[0];
}

function isManuscriptPath(path: string): boolean {
  return manuscriptFilter[0].extensions.includes(extname(path).toLocaleLowerCase("en").slice(1));
}

function inputError(message: string): never {
  throw new DesktopInputError("DESKTOP_INPUT_INVALID", message);
}

function oneArgument(args: readonly unknown[], label: string): unknown {
  if (args.length !== 1) {
    return inputError(`${label} requires exactly one payload`);
  }
  return args[0];
}

function noArguments(args: readonly unknown[], label: string): void {
  if (args.length !== 0) {
    inputError(`${label} does not accept a payload`);
  }
}

function exactRecord(value: unknown, label: string, allowed: readonly string[]): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return inputError(`${label} must be a JSON object`);
  }
  const record = value as Record<string, unknown>;
  if (Object.keys(record).some((key) => !allowed.includes(key))) {
    return inputError(`${label} contains an unsupported field`);
  }
  return record;
}

function requiredText(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    return inputError(`${label} must be a non-empty string`);
  }
  return value.trim();
}

function optionalText(value: unknown, label: string): string | undefined {
  if (value === undefined) {
    return undefined;
  }
  return requiredText(value, label);
}

function requiredProviderId(value: unknown): ProviderId {
  const providerId = requiredText(value, "providerId");
  if (!PROVIDER_ID.test(providerId)) {
    return inputError("providerId contains unsupported characters");
  }
  return providerId as ProviderId;
}

function discoverModelsRequest(value: unknown): ServiceDiscoverModelsRequest {
  const record = exactRecord(value, "discover-models payload", ["providerId", "apiKey", "customBaseUrl"]);
  const providerId = requiredProviderId(record.providerId);
  const apiKey = optionalText(record.apiKey, "apiKey");
  const customBaseUrl = optionalText(record.customBaseUrl, "customBaseUrl");
  return {
    providerId,
    ...(apiKey === undefined ? {} : { apiKey }),
    ...(customBaseUrl === undefined ? {} : { customBaseUrl }),
  };
}

function testModelRequest(value: unknown): ServiceTestModelRequest {
  const record = exactRecord(value, "test-model payload", [
    "providerId",
    "apiKey",
    "modelId",
    "reasoningEffort",
    "customBaseUrl",
  ]);
  const providerId = requiredProviderId(record.providerId);
  const apiKey = optionalText(record.apiKey, "apiKey");
  const modelId = requiredText(record.modelId, "modelId");
  const reasoningEffort = optionalText(record.reasoningEffort, "reasoningEffort");
  const customBaseUrl = optionalText(record.customBaseUrl, "customBaseUrl");
  return {
    providerId,
    modelId,
    ...(apiKey === undefined ? {} : { apiKey }),
    ...(reasoningEffort === undefined ? {} : { reasoningEffort: reasoningEffort as ProviderEffort }),
    ...(customBaseUrl === undefined ? {} : { customBaseUrl }),
  };
}

function confirmSourceEncodingRequest(value: unknown): DesktopConfirmSourceEncodingRequest {
  const input = exactRecord(
    value,
    "confirm-source-encoding payload",
    ["pendingImportId", "encoding"],
  );
  const pendingImportId = requiredText(input.pendingImportId, "pendingImportId");
  const encoding = requiredText(input.encoding, "encoding");
  if (!SOURCE_ENCODINGS.has(encoding)) {
    return inputError("encoding is unsupported");
  }
  return {
    pendingImportId,
    encoding: encoding as DesktopConfirmSourceEncodingRequest["encoding"],
  };
}

function startTrialRequest(value: unknown): DesktopStartTrialRequest {
  const input = exactRecord(value, "start-trial payload", ["mode"]);
  const mode = requiredText(input.mode, "mode");
  if (mode !== "quality" && mode !== "fast") {
    return inputError("mode must be quality or fast");
  }
  return { mode };
}

function publicProviders(snapshot: DesktopIpcModelSnapshot): readonly DesktopOnboardingProvider[] {
  return snapshot.providers.map((provider) => ({
    id: provider.id,
    displayName: provider.displayName,
    keyPlaceholder: provider.keyPlaceholder,
    efforts: [...provider.efforts],
    fallbackModelIds: [...provider.fallbackModelIds],
    allowManualModel: provider.allowManualModel,
    allowCustomBaseUrl: provider.allowCustomBaseUrl,
    credentialStatus: provider.credentialStatus,
    ...(provider.credentialPersistence === undefined
      ? {}
      : { credentialPersistence: provider.credentialPersistence }),
  }));
}

function publicProbe(probe: DesktopIpcModelSnapshot["latestProbe"]): DesktopModelProbe | undefined {
  if (probe === undefined) {
    return undefined;
  }
  return {
    status: probe.status,
    ...(probe.providerId === undefined ? {} : { providerId: probe.providerId }),
    ...(probe.modelId === undefined ? {} : { modelId: probe.modelId }),
    ...(probe.code === undefined ? {} : { code: probe.code }),
    ...(probe.message === undefined ? {} : { message: probe.message }),
    ...(probe.retryable === undefined ? {} : { retryable: probe.retryable }),
    ...(probe.checkedAt === undefined ? {} : { checkedAt: probe.checkedAt }),
  };
}

function onboardingState(
  modelSnapshot: DesktopIpcModelSnapshot,
  project: DesktopProjectSnapshot | undefined,
): DesktopOnboardingState {
  const providers = publicProviders(modelSnapshot);
  const latestProbe = publicProbe(modelSnapshot.latestProbe);
  const profile = modelSnapshot.activeModelProfile;
  const provider = profile === undefined
    ? undefined
    : providers.find((candidate) => candidate.id === profile.providerId);
  // `latestProbe` describes the most recent attempted model, which can differ
  // from the separately persisted ready profile. Credential availability is
  // the durable fact that the saved profile remains runnable.
  const capability: DesktopModelSummary["capability"] = profile !== undefined
    && provider?.credentialStatus === "available"
    ? "ready"
    : "unverified";
  const activeModel = profile === undefined
    ? undefined
    : {
      providerId: profile.providerId,
      modelId: profile.modelId,
      ...(profile.reasoningEffort === undefined ? {} : { reasoningEffort: profile.reasoningEffort }),
      // Only the explicitly configured custom endpoint can return to the renderer.
      ...(provider?.allowCustomBaseUrl === true && profile.customBaseUrl !== undefined
        ? { customBaseUrl: profile.customBaseUrl }
        : {}),
      capability,
    };
  const model = activeModel?.capability === "ready";
  return {
    ...(project === undefined ? {} : { project }),
    providers,
    ...(activeModel === undefined ? {} : { activeModel }),
    ...(latestProbe === undefined ? {} : { latestProbe }),
    readiness: {
      source: project !== undefined,
      model,
      trial: project !== undefined && model,
    },
  };
}

export function registerDesktopIpc(dependencies: DesktopIpcDependencies): void {
  let activeSnapshot: DesktopProjectSnapshot | undefined;

  const handleTrusted = (channel: DesktopIpcChannel, handler: DesktopIpcHandler): void => {
    dependencies.ipcMain.handle(channel, async (event, ...args) => {
      try {
        if (!dependencies.isTrustedEvent(event)) {
          return untrustedEvent();
        }
      } catch {
        return untrustedEvent();
      }
      return handler(event, ...args);
    });
  };

  const snapshot = (request: DesktopProjectRequest): DesktopResult<DesktopProjectSnapshot> => {
    const result = dependencies.projectService.snapshot(request);
    if (result.ok) {
      activeSnapshot = result.value;
    }
    return result;
  };

  const activateImportedSource = (
    imported: Pick<DesktopSourceReadyResult, "manifestPath">,
  ): DesktopResult<DesktopChooseSourceResult> => {
    const request: DesktopProjectRequest = { manifestPath: imported.manifestPath };
    const result = snapshot(request);
    if (!result.ok) {
      return fail(result.error);
    }
    dependencies.setCurrentRequest(request);
    return ok({ status: "ready", project: result.value });
  };

  const currentProject = (): DesktopResult<DesktopProjectSnapshot | undefined> => {
    const current = dependencies.getCurrentRequest();
    if (current === undefined) {
      return ok(undefined);
    }
    const result = snapshot(current);
    return result.ok ? ok(result.value) : fail(result.error);
  };

  const currentOnboarding = (): DesktopResult<DesktopOnboardingState> => {
    const project = currentProject();
    if (!project.ok) {
      return fail(project.error);
    }
    return ok(onboardingState(dependencies.modelService.snapshot(), project.value));
  };

  handleTrusted("folioloom:choose-source", async (_event, ...args) => resultFrom(async () => {
    noArguments(args, "choose-source");
    const sourcePath = await chooseSingleFile(dependencies.dialog, manuscriptFilter);
    if (sourcePath === undefined) {
      return canceledSelection();
    }
    if (!isManuscriptPath(sourcePath)) {
      return invalidSelection("sourcePath must use a supported manuscript extension");
    }
    const imported = await dependencies.sourceService.importSource({ sourcePath });
    if (imported.status === "encoding_required") {
      return ok({
        status: imported.status,
        pendingImportId: imported.pendingImportId,
        fileName: imported.fileName,
        encodings: [...imported.encodings],
      });
    }
    return activateImportedSource(imported);
  }));

  handleTrusted("folioloom:confirm-source-encoding", async (_event, ...args) => resultFrom(async () => {
    const request = confirmSourceEncodingRequest(oneArgument(args, "confirm-source-encoding"));
    const imported = await dependencies.sourceService.confirmEncoding(request);
    return activateImportedSource(imported);
  }));

  handleTrusted("folioloom:onboarding-state", async (_event, ...args) => resultFrom(() => {
    noArguments(args, "onboarding-state");
    return currentOnboarding();
  }));

  handleTrusted("folioloom:discover-models", async (_event, ...args) => resultFrom(async () => {
    const request = discoverModelsRequest(oneArgument(args, "discover-models"));
    const models = await dependencies.modelService.discoverModels(request);
    const value: readonly DesktopModelOption[] = models.map((model) => ({
      id: model.id,
      displayName: model.displayName,
    }));
    return ok(value);
  }));

  handleTrusted("folioloom:test-model", async (_event, ...args) => resultFrom(async () => {
    const request = testModelRequest(oneArgument(args, "test-model"));
    const tested = await dependencies.modelService.testAndSave(request);
    const project = currentProject();
    if (!project.ok) {
      return fail(project.error);
    }
    const report = publicProbe(tested.report);
    if (report === undefined) {
      return failure("DESKTOP_MODEL_TEST_INVALID", "model test returned no report");
    }
    const value: DesktopTestModelResult = {
      report,
      onboarding: onboardingState(tested.snapshot, project.value),
    };
    return ok(value);
  }));

  handleTrusted("folioloom:forget-credential", async (_event, ...args) => resultFrom(() => {
    const providerId = requiredProviderId(oneArgument(args, "forget-credential"));
    dependencies.modelService.forgetCredential(providerId);
    return currentOnboarding();
  }));

  handleTrusted("folioloom:start-trial", async (_event, ...args) => resultFrom(async () => {
    const request = startTrialRequest(oneArgument(args, "start-trial"));
    const current = dependencies.getCurrentRequest();
    if (current === undefined) {
      return failure("DESKTOP_NO_PROJECT", "choose a manuscript before starting a trial");
    }
    return ok(await dependencies.trialService.start({
      manifestPath: current.manifestPath,
      mode: request.mode,
    }));
  }));

  handleTrusted("folioloom:cancel-trial", async (_event, ...args) => resultFrom(async () => {
    noArguments(args, "cancel-trial");
    await dependencies.trialService.cancel();
    return ok(undefined);
  }));

  handleTrusted("folioloom:choose-project", async () => resultFrom(async () => {
    const manifestPath = await chooseSingleFile(dependencies.dialog, manifestFilter);
    if (manifestPath === undefined) {
      return canceledSelection();
    }
    if (basename(manifestPath) !== "source_manifest.json") {
      return invalidSelection("manifestPath must identify source_manifest.json");
    }
    const request: DesktopProjectRequest = { manifestPath };
    const result = snapshot(request);
    if (result.ok) {
      dependencies.setCurrentRequest(request);
    }
    return result;
  }));

  handleTrusted("folioloom:choose-store", async () => resultFrom(async () => {
    const current = dependencies.getCurrentRequest();
    if (current === undefined) {
      return noOpenProject();
    }
    const storePath = await chooseSingleFile(dependencies.dialog, storeFilter);
    if (storePath === undefined) {
      return canceledSelection();
    }
    const request: DesktopProjectRequest = { ...current, storePath, runId: undefined };
    const result = snapshot(request);
    if (result.ok) {
      dependencies.setCurrentRequest(request);
    }
    return result;
  }));

  handleTrusted("folioloom:refresh-project", async () => resultFrom(() => {
    const current = dependencies.getCurrentRequest();
    return current === undefined ? noOpenProject() : snapshot(current);
  }));

  handleTrusted("folioloom:select-run", async (_event, runId) => resultFrom(() => {
    const current = dependencies.getCurrentRequest();
    if (current === undefined || activeSnapshot === undefined) {
      return noOpenProject();
    }
    if (typeof runId !== "string" || !activeSnapshot.runs.some((run) => run.runId === runId)) {
      return invalidSelection("runId must identify a run in the active project snapshot");
    }
    const request: DesktopProjectRequest = { ...current, runId };
    const result = snapshot(request);
    if (result.ok) {
      dependencies.setCurrentRequest(request);
    }
    return result;
  }));

  handleTrusted("folioloom:doctor", async () => resultFrom(() => {
    const current = dependencies.getCurrentRequest();
    if (current === undefined) {
      return failure("DESKTOP_NO_PROJECT", "open an initialized project first");
    }
    return dependencies.projectService.doctor({
      manifestPath: current.manifestPath,
      ...(current.glossaryPath === undefined ? {} : { glossaryPath: current.glossaryPath }),
    });
  }));
}
