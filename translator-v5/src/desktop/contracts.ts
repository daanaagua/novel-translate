export type DesktopResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: DesktopError };

export interface DesktopError {
  code: string;
  message: string;
  retryable: boolean;
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
