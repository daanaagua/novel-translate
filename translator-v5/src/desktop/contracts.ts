export type DesktopResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: DesktopError };

export interface DesktopError {
  code: string;
  message: string;
  nextAction?: string;
  retryable: boolean;
  technicalDetails?: string;
}

export interface DesktopRunSummary {
  runId: string;
  sourceVersion: string;
  modelId: string;
  status: string;
  progress: {
    totalWindows: number;
    pendingWindows: number;
    completedWindows: number;
    warningWindows: number;
    humanRequiredWindows: number;
    failedWindows: number;
  };
}

export interface DesktopProjectSnapshot {
  manifestPath: string;
  title: string;
  sourceLanguage: string;
  sourceChars: number;
  sourceVersion: string;
  glossaryPath?: string;
  store: {
    state: "not_found" | "ready" | "invalid";
    path?: string;
    error?: DesktopError;
  };
  runs: DesktopRunSummary[];
  selectedRunId?: string;
  runSelection: "none" | "selected" | "required";
}

export interface DesktopDoctorReport {
  sourceVersion: string;
  sourceChars: number;
  coveredChars: number;
  annotationCount: number;
  blockCount: number;
  windowCount: number;
  incidentCodes: string[];
  anomalyCount: number;
  glossary?: {
    path: string;
    totalTerms: number;
    matchedTerms: number;
    unmatchedTerms: number;
    unmatchedForms: string[];
  };
}

export interface DesktopProjectRequest {
  manifestPath: string;
  storePath?: string;
  runId?: string;
  glossaryPath?: string;
}

export interface DesktopRecentProject {
  manifestPath: string;
  storePath?: string;
  runId?: string;
}

/**
 * Renderer-facing provider data. It intentionally does not include a preset
 * base URL, a runtime instance, or any form of credential.
 */
export interface DesktopOnboardingProvider {
  id: string;
  displayName: string;
  keyPlaceholder: string;
  efforts: readonly string[];
  fallbackModelIds: readonly string[];
  allowManualModel: boolean;
  allowCustomBaseUrl: boolean;
  credentialStatus: "available" | "missing" | "needs_reentry";
  credentialPersistence?: "encrypted" | "session";
}

export interface DesktopModelOption {
  id: string;
  displayName: string;
}

export interface DesktopModelProbe {
  status: "ready" | "limited" | "failed";
  code?: string;
  message?: string;
  retryable?: boolean;
  checkedAt?: string;
}

export interface DesktopModelSummary {
  providerId: string;
  modelId: string;
  reasoningEffort?: string;
  customBaseUrl?: string;
  capability: DesktopModelProbe["status"] | "unverified";
}

export interface DesktopOnboardingState {
  project?: DesktopProjectSnapshot;
  providers: readonly DesktopOnboardingProvider[];
  activeModel?: DesktopModelSummary;
  latestProbe?: DesktopModelProbe;
  readiness: {
    source: boolean;
    model: boolean;
    trial: boolean;
  };
}

/** A one-shot credential may enter only through a request, never a response. */
export interface DesktopDiscoverModelsRequest {
  providerId: string;
  apiKey?: string;
  customBaseUrl?: string;
}

export interface DesktopTestModelRequest {
  providerId: string;
  apiKey?: string;
  modelId: string;
  reasoningEffort?: string;
  customBaseUrl?: string;
}

export interface DesktopTestModelResult {
  report: DesktopModelProbe;
  onboarding: DesktopOnboardingState;
}
