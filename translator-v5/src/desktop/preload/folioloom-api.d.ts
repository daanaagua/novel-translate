import type {
  DesktopDiscoverModelsRequest,
  DesktopDoctorReport,
  DesktopModelOption,
  DesktopOnboardingState,
  DesktopProjectSnapshot,
  DesktopResult,
  DesktopTestModelRequest,
  DesktopTestModelResult,
} from "../contracts.js";

export interface FolioLoomDesktopApi {
  chooseSource(): Promise<DesktopResult<DesktopProjectSnapshot>>;
  getOnboardingState(): Promise<DesktopResult<DesktopOnboardingState>>;
  discoverModels(request: DesktopDiscoverModelsRequest): Promise<DesktopResult<readonly DesktopModelOption[]>>;
  testModel(request: DesktopTestModelRequest): Promise<DesktopResult<DesktopTestModelResult>>;
  forgetCredential(providerId: string): Promise<DesktopResult<DesktopOnboardingState>>;
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
