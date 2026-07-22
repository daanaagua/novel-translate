import { useEffect, useMemo, useState, type JSX } from "react";

import type {
  DesktopChooseSourceResult,
  DesktopSourceEncoding,
  DesktopSourceEncodingRequired,
  DesktopDiscoverModelsRequest,
  DesktopError,
  DesktopModelOption,
  DesktopOnboardingState,
  DesktopResult,
  DesktopTestModelRequest,
  DesktopTestModelResult,
  DesktopTrialProgress,
  DesktopTrialResult,
} from "../../contracts.js";
import type { FolioLoomDesktopApi } from "../../preload/folioloom-api.js";
import { Onboarding } from "./components/Onboarding.js";
import { Sidebar } from "./components/Sidebar.js";
import { WindowTitlebar } from "./components/WindowTitlebar.js";
import { WorkspacePlaceholder } from "./components/WorkspacePlaceholder.js";
import type { BusyAction, WorkspaceId } from "./types.js";

interface AppProps {
  api?: FolioLoomDesktopApi;
}

const emptyOnboarding: DesktopOnboardingState = {
  providers: [],
  readiness: { source: false, model: false, trial: false },
};

function unavailableResult<T = never>(): DesktopResult<T> {
  return {
    ok: false,
    error: {
      code: "DESKTOP_UNAVAILABLE",
      message: "此功能暂时不可用。",
      retryable: true,
    },
  };
}

function unavailableApi(): FolioLoomDesktopApi {
  return {
    chooseSource: async () => unavailableResult(),
    confirmSourceEncoding: async () => unavailableResult(),
    getOnboardingState: async () => unavailableResult(),
    discoverModels: async () => unavailableResult(),
    testModel: async () => unavailableResult(),
    forgetCredential: async () => unavailableResult(),
    startTrial: async () => unavailableResult(),
    cancelTrial: async () => unavailableResult(),
    onTrialProgress: () => () => undefined,
    chooseProject: async () => unavailableResult(),
    chooseStore: async () => unavailableResult(),
    refreshProject: async () => unavailableResult(),
    selectRun: async () => unavailableResult(),
    runDoctor: async () => unavailableResult(),
  };
}

function browserApi(): FolioLoomDesktopApi {
  if (typeof window === "undefined" || window.folioLoom === undefined) return unavailableApi();
  return window.folioLoom;
}

function errorFromUnknown(error: unknown): DesktopError {
  return {
    code: "DESKTOP_RENDERER_ERROR",
    message: "操作未完成，请重试。",
    retryable: true,
    ...(error instanceof Error && error.message !== "" ? { technicalDetails: error.message } : {}),
  };
}

export function App({ api }: AppProps): JSX.Element {
  const fallbackApi = useMemo(browserApi, []);
  const desktopApi = api ?? fallbackApi;
  const [activeWorkspace, setActiveWorkspace] = useState<WorkspaceId>("overview");
  const [onboarding, setOnboarding] = useState<DesktopOnboardingState>(emptyOnboarding);
  const [busyAction, setBusyAction] = useState<BusyAction>();
  const [operationError, setOperationError] = useState<DesktopError>();
  const [trialProgress, setTrialProgress] = useState<DesktopTrialProgress>();
  const [trialResult, setTrialResult] = useState<DesktopTrialResult>();
  const [pendingEncoding, setPendingEncoding] = useState<DesktopSourceEncodingRequired>();

  function acceptOnboarding(result: DesktopResult<DesktopOnboardingState>): boolean {
    if (!result.ok) {
      setOperationError(result.error);
      return false;
    }
    setOnboarding(result.value);
    setOperationError(undefined);
    return true;
  }

  useEffect(() => {
    let disposed = false;
    void desktopApi.getOnboardingState()
      .then((result) => {
        if (!disposed) acceptOnboarding(result);
      })
      .catch((error: unknown) => {
        if (!disposed) setOperationError(errorFromUnknown(error));
      });
    return () => {
      disposed = true;
    };
  }, [desktopApi]);

  useEffect(() => desktopApi.onTrialProgress((progress) => {
    setTrialProgress(progress);
  }), [desktopApi]);

  async function acceptSourceChoice(
    choice: DesktopChooseSourceResult,
  ): Promise<void> {
    if (choice.status === "encoding_required") {
      setPendingEncoding(choice);
      setOperationError(undefined);
      return;
    }
    setPendingEncoding(undefined);
    const stateResult = await desktopApi.getOnboardingState();
    if (!stateResult.ok) {
      setOnboarding((current) => ({
        ...current,
        project: choice.project,
        readiness: { ...current.readiness, source: true, trial: false },
      }));
      setOperationError(stateResult.error);
      return;
    }
    acceptOnboarding(stateResult);
    setTrialProgress(undefined);
    setTrialResult(undefined);
  }

  async function chooseSource(): Promise<void> {
    setActiveWorkspace("overview");
    setPendingEncoding(undefined);
    setBusyAction("choose-source");
    try {
      const sourceResult = await desktopApi.chooseSource();
      if (!sourceResult.ok) {
        setOperationError(sourceResult.error);
        return;
      }

      await acceptSourceChoice(sourceResult.value);
    } catch (error) {
      setOperationError(errorFromUnknown(error));
    } finally {
      setBusyAction(undefined);
    }
  }

  async function confirmSourceEncoding(encoding: DesktopSourceEncoding): Promise<void> {
    if (pendingEncoding === undefined) return;
    setBusyAction("confirm-encoding");
    setOperationError(undefined);
    try {
      const result = await desktopApi.confirmSourceEncoding({
        pendingImportId: pendingEncoding.pendingImportId,
        encoding,
      });
      if (!result.ok) {
        setPendingEncoding(undefined);
        setOperationError(result.error);
        return;
      }
      await acceptSourceChoice(result.value);
    } catch (error) {
      setPendingEncoding(undefined);
      setOperationError(errorFromUnknown(error));
    } finally {
      setBusyAction(undefined);
    }
  }

  async function discoverModels(request: DesktopDiscoverModelsRequest): Promise<DesktopResult<readonly DesktopModelOption[]>> {
    setBusyAction("discover-models");
    try {
      const result = await desktopApi.discoverModels(request);
      if (!result.ok) setOperationError(result.error);
      else setOperationError(undefined);
      return result;
    } catch (error) {
      const rendererError = errorFromUnknown(error);
      setOperationError(rendererError);
      return { ok: false, error: rendererError };
    } finally {
      setBusyAction(undefined);
    }
  }

  async function testModel(request: DesktopTestModelRequest): Promise<DesktopResult<DesktopTestModelResult>> {
    setBusyAction("test-model");
    try {
      const result = await desktopApi.testModel(request);
      if (!result.ok) {
        setOperationError(result.error);
        return result;
      }
      setOnboarding(result.value.onboarding);
      setOperationError(undefined);
      return result;
    } catch (error) {
      const rendererError = errorFromUnknown(error);
      setOperationError(rendererError);
      return { ok: false, error: rendererError };
    } finally {
      setBusyAction(undefined);
    }
  }

  async function forgetCredential(providerId: string): Promise<DesktopResult<DesktopOnboardingState>> {
    setBusyAction("forget-credential");
    try {
      const result = await desktopApi.forgetCredential(providerId);
      if (!result.ok) {
        setOperationError(result.error);
        return result;
      }
      setOnboarding(result.value);
      setOperationError(undefined);
      return result;
    } catch (error) {
      const rendererError = errorFromUnknown(error);
      setOperationError(rendererError);
      return { ok: false, error: rendererError };
    } finally {
      setBusyAction(undefined);
    }
  }

  async function startTrial(): Promise<void> {
    setBusyAction("start-trial");
    setTrialResult(undefined);
    setOperationError(undefined);
    try {
      const result = await desktopApi.startTrial();
      if (!result.ok) {
        if (result.error.code === "DESKTOP_TRIAL_CANCELLED") {
          setTrialProgress(undefined);
        } else {
          setOperationError(result.error);
        }
        return;
      }
      setTrialResult(result.value);
      setTrialProgress({ stage: "completed" });
    } catch (error) {
      setOperationError(errorFromUnknown(error));
    } finally {
      setBusyAction(undefined);
    }
  }

  async function cancelTrial(): Promise<void> {
    setBusyAction("cancel-trial");
    try {
      const result = await desktopApi.cancelTrial();
      if (!result.ok) setOperationError(result.error);
      else setTrialProgress(undefined);
    } catch (error) {
      setOperationError(errorFromUnknown(error));
    } finally {
      setBusyAction(undefined);
    }
  }

  return (
    <div className="workbench-shell">
      <WindowTitlebar />
      <Sidebar
        activeWorkspace={activeWorkspace}
        hasProject={onboarding.project !== undefined}
        onSelectWorkspace={setActiveWorkspace}
      />
      <div className="workbench-main">
        {activeWorkspace === "overview" ? (
          <Onboarding
            onboarding={onboarding}
            busyAction={busyAction}
            operationError={operationError}
            trialProgress={trialProgress}
            trialResult={trialResult}
            pendingEncoding={pendingEncoding}
            onChooseSource={chooseSource}
            onConfirmSourceEncoding={confirmSourceEncoding}
            onDiscoverModels={discoverModels}
            onTestModel={testModel}
            onForgetCredential={forgetCredential}
            onStartTrial={startTrial}
            onCancelTrial={cancelTrial}
          />
        ) : (
          <WorkspacePlaceholder
            workspace={activeWorkspace}
            snapshot={onboarding.project}
            onChooseProject={() => { void chooseSource(); }}
          />
        )}
      </div>
    </div>
  );
}
