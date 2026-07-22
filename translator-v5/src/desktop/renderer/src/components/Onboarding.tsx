import type { JSX } from "react";

import type {
  DesktopDiscoverModelsRequest,
  DesktopError,
  DesktopModelOption,
  DesktopOnboardingState,
  DesktopResult,
  DesktopTestModelRequest,
  DesktopTestModelResult,
  DesktopTrialProgress,
  DesktopTrialResult,
} from "../../../contracts.js";
import type { BusyAction } from "../types.js";
import { ProviderSetup } from "./ProviderSetup.js";
import { redactTechnicalDetails, TechnicalDetails } from "./TechnicalDetails.js";

interface OnboardingProps {
  onboarding: DesktopOnboardingState;
  busyAction: BusyAction;
  operationError?: DesktopError;
  trialProgress?: DesktopTrialProgress;
  trialResult?: DesktopTrialResult;
  onChooseSource(): Promise<void>;
  onDiscoverModels(request: DesktopDiscoverModelsRequest): Promise<DesktopResult<readonly DesktopModelOption[]>>;
  onTestModel(request: DesktopTestModelRequest): Promise<DesktopResult<DesktopTestModelResult>>;
  onForgetCredential(providerId: string): Promise<DesktopResult<DesktopOnboardingState>>;
  onStartTrial(): Promise<void>;
  onCancelTrial(): Promise<void>;
}

function formatChars(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

const TRIAL_STAGE_LABELS: Record<DesktopTrialProgress["stage"], string> = {
  preparing: "正在准备书稿",
  translating: "翻译中",
  checking: "正在检查译文",
  completed: "试译完成",
  failed: "试译未完成",
};

export function Onboarding({
  onboarding,
  busyAction,
  operationError,
  trialProgress,
  trialResult,
  onChooseSource,
  onDiscoverModels,
  onTestModel,
  onForgetCredential,
  onStartTrial,
  onCancelTrial,
}: OnboardingProps): JSX.Element {
  const sourceReady = onboarding.project !== undefined;
  const modelReady = onboarding.activeModel?.capability === "ready";
  const trialEnabled = sourceReady && modelReady && busyAction === undefined;
  const trialRunning = busyAction === "start-trial" || busyAction === "cancel-trial";

  return (
    <main className="onboarding-scroll">
      <div className="onboarding-column">
        <header className="onboarding-header">
          <p className="eyebrow">FolioLoom</p>
          <h1>开始翻译一本书</h1>
          <p className="onboarding-lead">先选入原文，再接上你自己的模型。准备好后，从一小段试译开始。</p>
        </header>

        <section className={`setup-step${sourceReady ? " is-complete" : ""}`} aria-labelledby="source-step-title">
          <div className="step-marker" aria-hidden="true">1</div>
          <div className="step-content">
            <p className="eyebrow">书稿</p>
            <h2 id="source-step-title">选择要翻译的书稿</h2>
            {sourceReady && onboarding.project !== undefined ? (
              <div className="source-summary">
                <strong>{onboarding.project.title}</strong>
                <span>{onboarding.project.sourceLanguage} · {formatChars(onboarding.project.sourceChars)} 字符</span>
              </div>
            ) : (
              <p className="section-copy">支持 TXT、EPUB、DOCX 和 Markdown。</p>
            )}
            <button
              className="primary-button"
              type="button"
              disabled={busyAction !== undefined}
              onClick={() => { void onChooseSource(); }}
            >
              {busyAction === "choose-source" ? "正在选择…" : "选择书稿"}
            </button>
          </div>
        </section>

        <section className={`setup-step${modelReady ? " is-complete" : ""}`} aria-labelledby="model-step-title">
          <div className="step-marker" aria-hidden="true">2</div>
          <div className="step-content">
            <p className="eyebrow">模型</p>
            <h2 id="model-step-title">连接你的模型</h2>
            <p className="section-copy">密钥只用于当前操作；你可以随时更换服务或重新测试。</p>
            <ProviderSetup
              providers={onboarding.providers}
              activeModel={onboarding.activeModel}
              busy={busyAction !== undefined}
              onDiscoverModels={onDiscoverModels}
              onTestModel={onTestModel}
              onForgetCredential={onForgetCredential}
            />
          </div>
        </section>

        <section className={`setup-step${trialResult !== undefined ? " is-complete" : ""}`} aria-labelledby="trial-step-title">
          <div className="step-marker" aria-hidden="true">3</div>
          <div className="step-content">
            <p className="eyebrow">试译</p>
            <h2 id="trial-step-title">先试译一小段</h2>
            <p className="section-copy">确认书稿和模型后，再开始阅读和翻译。</p>
            {trialProgress === undefined ? null : (
              <p className={`trial-status is-${trialProgress.stage}`} role="status">
                {TRIAL_STAGE_LABELS[trialProgress.stage]}
              </p>
            )}
            <div className="trial-actions">
              <button
                className="primary-button"
                type="button"
                data-action="start-trial"
                disabled={!trialEnabled}
                onClick={() => { void onStartTrial(); }}
              >
                {trialRunning ? "试译进行中…" : "开始试译"}
              </button>
              {!trialRunning ? null : (
                <button
                  className="quiet-button"
                  type="button"
                  disabled={busyAction === "cancel-trial"}
                  onClick={() => { void onCancelTrial(); }}
                >
                  {busyAction === "cancel-trial" ? "正在取消…" : "取消试译"}
                </button>
              )}
            </div>
            {trialResult === undefined ? null : (
              <div className="trial-result" aria-label="试译结果">
                <article>
                  <p className="field-label">原文</p>
                  <p className="trial-text">{trialResult.sourceText}</p>
                </article>
                <article>
                  <p className="field-label">译文</p>
                  <p className="trial-text">{trialResult.translationText}</p>
                </article>
              </div>
            )}
          </div>
        </section>

        {operationError === undefined ? null : (
          <section className="operation-error" role="status">
            <p>{redactTechnicalDetails(operationError.message)}</p>
            {operationError.nextAction === undefined ? null : <p>{redactTechnicalDetails(operationError.nextAction)}</p>}
            <TechnicalDetails details={operationError.technicalDetails} />
          </section>
        )}
      </div>
    </main>
  );
}
