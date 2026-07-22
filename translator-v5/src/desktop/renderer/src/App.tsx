import { useEffect, useMemo, useState, type JSX } from "react";

import type {
  DesktopDoctorReport,
  DesktopError,
  DesktopProjectSnapshot,
  DesktopResult,
} from "../../contracts.js";
import type { FolioLoomDesktopApi } from "../../preload/folioloom-api.js";
import { ProjectOverview } from "./components/ProjectOverview.js";
import { Sidebar } from "./components/Sidebar.js";
import { WindowTitlebar } from "./components/WindowTitlebar.js";
import { WorkspacePlaceholder } from "./components/WorkspacePlaceholder.js";
import type { BusyAction, WorkspaceId } from "./types.js";

interface AppProps {
  api?: FolioLoomDesktopApi;
}

function noProjectResult<T = never>(): DesktopResult<T> {
  return {
    ok: false,
    error: {
      code: "DESKTOP_NO_PROJECT",
      message: "open an initialized project first",
      retryable: false,
    },
  };
}

function unavailableApi(): FolioLoomDesktopApi {
  return {
    chooseProject: async () => noProjectResult(),
    chooseStore: async () => noProjectResult(),
    refreshProject: async () => noProjectResult(),
    selectRun: async () => noProjectResult(),
    runDoctor: async () => noProjectResult(),
  };
}

function browserApi(): FolioLoomDesktopApi {
  if (typeof window === "undefined" || window.folioLoom === undefined) {
    return unavailableApi();
  }
  return window.folioLoom;
}

function errorFromUnknown(error: unknown): DesktopError {
  if (error instanceof Error) {
    return { code: "DESKTOP_RENDERER_ERROR", message: error.message, retryable: false };
  }
  return { code: "DESKTOP_RENDERER_ERROR", message: "unexpected renderer error", retryable: false };
}

export function App({ api }: AppProps): JSX.Element {
  const fallbackApi = useMemo(browserApi, []);
  const desktopApi = api ?? fallbackApi;
  const [activeWorkspace, setActiveWorkspace] = useState<WorkspaceId>("overview");
  const [snapshot, setSnapshot] = useState<DesktopProjectSnapshot>();
  const [doctorResult, setDoctorResult] = useState<DesktopResult<DesktopDoctorReport>>();
  const [busyAction, setBusyAction] = useState<BusyAction>();
  const [operationError, setOperationError] = useState<DesktopError>();

  function acceptSnapshot(result: DesktopResult<DesktopProjectSnapshot>): void {
    if (result.ok) {
      setSnapshot(result.value);
      setDoctorResult(undefined);
      setOperationError(undefined);
      return;
    }
    if (result.error.code === "DESKTOP_NO_PROJECT") {
      setSnapshot(undefined);
      setOperationError(undefined);
      return;
    }
    setOperationError(result.error);
  }

  useEffect(() => {
    let disposed = false;
    void desktopApi.refreshProject()
      .then((result) => {
        if (!disposed) acceptSnapshot(result);
      })
      .catch((error: unknown) => {
        if (!disposed) setOperationError(errorFromUnknown(error));
      });
    return () => {
      disposed = true;
    };
  }, [desktopApi]);

  async function runProjectAction(
    action: Exclude<BusyAction, "doctor" | undefined>,
    operation: () => Promise<DesktopResult<DesktopProjectSnapshot>>,
  ): Promise<void> {
    setBusyAction(action);
    try {
      acceptSnapshot(await operation());
    } catch (error) {
      setOperationError(errorFromUnknown(error));
    } finally {
      setBusyAction(undefined);
    }
  }

  async function chooseProject(): Promise<void> {
    setActiveWorkspace("overview");
    await runProjectAction("choose-project", () => desktopApi.chooseProject());
  }

  async function chooseStore(): Promise<void> {
    await runProjectAction("choose-store", () => desktopApi.chooseStore());
  }

  async function refreshProject(): Promise<void> {
    await runProjectAction("refresh", () => desktopApi.refreshProject());
  }

  async function selectRun(runId: string): Promise<void> {
    await runProjectAction("select-run", () => desktopApi.selectRun(runId));
  }

  async function runDoctor(): Promise<void> {
    setBusyAction("doctor");
    try {
      const result = await desktopApi.runDoctor();
      setDoctorResult(result);
      if (result.ok) setOperationError(undefined);
    } catch (error) {
      setDoctorResult({ ok: false, error: errorFromUnknown(error) });
    } finally {
      setBusyAction(undefined);
    }
  }

  return (
    <div className="workbench-shell">
      <WindowTitlebar />
      <Sidebar
        activeWorkspace={activeWorkspace}
        hasProject={snapshot !== undefined}
        onSelectWorkspace={setActiveWorkspace}
      />
      <div className="workbench-main">
        {activeWorkspace === "overview" ? (
          <ProjectOverview
            snapshot={snapshot}
            doctorResult={doctorResult}
            busyAction={busyAction}
            operationError={operationError}
            onChooseProject={() => { void chooseProject(); }}
            onChooseStore={() => { void chooseStore(); }}
            onRefresh={() => { void refreshProject(); }}
            onSelectRun={(runId) => { void selectRun(runId); }}
            onRunDoctor={() => { void runDoctor(); }}
          />
        ) : (
          <WorkspacePlaceholder
            workspace={activeWorkspace}
            snapshot={snapshot}
            onChooseProject={() => { void chooseProject(); }}
          />
        )}
      </div>
    </div>
  );
}
