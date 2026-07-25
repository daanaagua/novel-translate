import { useEffect, useMemo, useRef, useState, type JSX } from "react";

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
  DesktopTrialMode,
  DesktopTrialProgress,
  DesktopTrialResult,
} from "../../contracts.js";
import type { FolioLoomDesktopApi } from "../../preload/folioloom-api.js";
import { Onboarding } from "./components/Onboarding.js";
import { KnowledgeWorkbench } from "./components/KnowledgeWorkbench.js";
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
    startTrial: async (_request) => unavailableResult(),
    cancelTrial: async () => unavailableResult(),
    onTrialProgress: () => () => undefined,
    getFullBookState: async () => unavailableResult(),
    startFullBook: async () => unavailableResult(),
    pauseFullBook: async () => unavailableResult(),
    resumeFullBook: async () => unavailableResult(),
    onFullBookProgress: () => () => undefined,
    getExportState: async () => unavailableResult(),
    chooseExportDirectory: async () => unavailableResult(),
    exportBook: async () => unavailableResult(),
    openExportDirectory: async () => unavailableResult(),
    listKnowledge: async () => unavailableResult(),
    getKnowledgeDetail: async () => unavailableResult(),
    mutateKnowledge: async () => unavailableResult(),
    promoteKnowledgeToGlobal: async () => unavailableResult(),
    listGlobalKnowledge: async () => unavailableResult(),
    attachGlobalKnowledge: async () => unavailableResult(),
    getKnowledgeDiagnostics: async () => unavailableResult(),
    chooseKnowledgeImport: async () => unavailableResult(),
    inspectKnowledgeImport: async () => unavailableResult(),
    confirmKnowledgeImportEncoding: async () => unavailableResult(),
    listStagedKnowledgeImports: async () => unavailableResult(),
    getStagedKnowledgeImport: async () => unavailableResult(),
    suggestKnowledgeImport: async () => unavailableResult(),
    stageKnowledgeImport: async () => unavailableResult(),
    decideKnowledgeImport: async () => unavailableResult(),
    commitKnowledgeImport: async () => unavailableResult(),
    rollbackKnowledgeImport: async () => unavailableResult(),
    cancelKnowledgeImportOperation: async () => unavailableResult(),
    cancelPendingKnowledgeImport: async () => unavailableResult(),
    discardStagedKnowledgeImport: async () => unavailableResult(),
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
  const onboardingGeneration = useRef(0);

  function beginOnboardingRefresh(): number {
    onboardingGeneration.current += 1;
    return onboardingGeneration.current;
  }

  function isCurrentOnboardingRefresh(generation: number): boolean {
    return generation === onboardingGeneration.current;
  }

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
    const generation = beginOnboardingRefresh();
    void desktopApi.getOnboardingState()
      .then((result) => {
        if (!disposed && isCurrentOnboardingRefresh(generation)) acceptOnboarding(result);
      })
      .catch((error: unknown) => {
        if (!disposed && isCurrentOnboardingRefresh(generation)) {
          setOperationError(errorFromUnknown(error));
        }
      });
    return () => {
      disposed = true;
    };
  }, [desktopApi]);

  useEffect(() => desktopApi.onTrialProgress((progress) => {
    setTrialProgress(progress);
  }), [desktopApi]);

  const knowledgeAvailable = onboarding.project?.store.state === "ready"
    && onboarding.project.selectedRunId !== undefined;

  useEffect(() => {
    if (activeWorkspace === "memory" && !knowledgeAvailable) {
      setActiveWorkspace("overview");
    }
  }, [activeWorkspace, knowledgeAvailable]);

  async function acceptSourceChoice(
    choice: DesktopChooseSourceResult,
  ): Promise<void> {
    if (choice.status === "encoding_required") {
      setPendingEncoding(choice);
      setOperationError(undefined);
      return;
    }
    setPendingEncoding(undefined);
    // A source identity change invalidates every projected trial artifact even
    // when refreshing the richer onboarding snapshot subsequently fails.
    setTrialProgress(undefined);
    setTrialResult(undefined);
    const generation = beginOnboardingRefresh();
    const stateResult = await desktopApi.getOnboardingState();
    if (!isCurrentOnboardingRefresh(generation)) return;
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
  }

  async function chooseSource(): Promise<void> {
    setActiveWorkspace("overview");
    setPendingEncoding(undefined);
    setBusyAction("choose-source");
    try {
      const sourceResult = await desktopApi.chooseSource();
      if (!sourceResult.ok) {
        if (sourceResult.error.code === "DESKTOP_SELECTION_CANCELLED") {
          setOperationError(undefined);
          return;
        }
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
        setOperationError(undefined);
        return result;
      }
      beginOnboardingRefresh();
      setOnboarding(result.value.onboarding);
      setOperationError(undefined);
      return result;
    } catch (error) {
      const rendererError = errorFromUnknown(error);
      setOperationError(undefined);
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
      beginOnboardingRefresh();
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

  async function startTrial(mode: DesktopTrialMode): Promise<void> {
    setBusyAction("start-trial");
    setTrialResult(undefined);
    setOperationError(undefined);
    try {
      const result = await desktopApi.startTrial({ mode });
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
        knowledgeAvailable={knowledgeAvailable}
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
        ) : activeWorkspace === "memory" && knowledgeAvailable ? (
          <KnowledgeWorkbench
            key={`${onboarding.project?.sourceVersion ?? "none"}:${onboarding.project?.selectedRunId ?? "none"}`}
            api={desktopApi}
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
