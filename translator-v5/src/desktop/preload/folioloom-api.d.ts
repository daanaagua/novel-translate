import type {
  DesktopChooseSourceResult,
  DesktopConfirmSourceEncodingRequest,
  DesktopDiscoverModelsRequest,
  DesktopDoctorReport,
  DesktopModelOption,
  DesktopOnboardingState,
  DesktopProjectSnapshot,
  DesktopResult,
  DesktopStartTrialRequest,
  DesktopTestModelRequest,
  DesktopTestModelResult,
  DesktopTrialProgress,
  DesktopTrialResult,
} from "../contracts.js";

export interface FolioLoomDesktopApi {
  chooseSource(): Promise<DesktopResult<DesktopChooseSourceResult>>;
  confirmSourceEncoding(
    request: DesktopConfirmSourceEncodingRequest,
  ): Promise<DesktopResult<DesktopChooseSourceResult>>;
  getOnboardingState(): Promise<DesktopResult<DesktopOnboardingState>>;
  discoverModels(request: DesktopDiscoverModelsRequest): Promise<DesktopResult<readonly DesktopModelOption[]>>;
  testModel(request: DesktopTestModelRequest): Promise<DesktopResult<DesktopTestModelResult>>;
  forgetCredential(providerId: string): Promise<DesktopResult<DesktopOnboardingState>>;
  startTrial(request: DesktopStartTrialRequest): Promise<DesktopResult<DesktopTrialResult>>;
  cancelTrial(): Promise<DesktopResult<void>>;
  onTrialProgress(listener: (progress: DesktopTrialProgress) => void): () => void;
  chooseProject(): Promise<DesktopResult<DesktopProjectSnapshot>>;
  chooseStore(): Promise<DesktopResult<DesktopProjectSnapshot>>;
  refreshProject(): Promise<DesktopResult<DesktopProjectSnapshot>>;
  selectRun(runId: string): Promise<DesktopResult<DesktopProjectSnapshot>>;
  runDoctor(): Promise<DesktopResult<DesktopDoctorReport>>;
}

declare global {
  interface Window {
    folioLoom: FolioLoomDesktopApi;
  }
}

export {};
