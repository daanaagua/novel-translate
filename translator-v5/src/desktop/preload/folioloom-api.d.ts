import type {
  DesktopDoctorReport,
  DesktopProjectSnapshot,
  DesktopResult,
} from "../contracts.js";

export interface FolioLoomDesktopApi {
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
