import { useState, type JSX } from "react";

import type {
  DesktopDiscoverModelsRequest,
  DesktopError,
  DesktopModelOption,
  DesktopOnboardingState,
  DesktopResult,
  DesktopSourceEncoding,
  DesktopSourceEncodingRequired,
  DesktopTestModelRequest,
  DesktopTestModelResult,
  DesktopTrialProgress,
  DesktopTrialResult,
} from "../../../contracts.js";
import type { BusyAction } from "../types.js";
import { ProviderSetup } from "./ProviderSetup.js";
import { EncodingChooser } from "./EncodingChooser.js";
import { redactTechnicalDetails, TechnicalDetails } from "./TechnicalDetails.js";

interface OnboardingProps {
  onboarding: DesktopOnboardingState;
  busyAction: BusyAction;
  operationError?: DesktopError;
  trialProgress?: DesktopTrialProgress;
  trialResult?: DesktopTrialResult;
  pendingEncoding?: DesktopSourceEncodingRequired;
  onChooseSource(): Promise<void>;
  onConfirmSourceEncoding(encoding: DesktopSourceEncoding): Promise<void>;
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
  pendingEncoding,
  onChooseSource,
  onConfirmSourceEncoding,
  onDiscoverModels,
  onTestModel,
  onForgetCredential,
  onStartTrial,
  onCancelTrial,
}: OnboardingProps): JSX.Element {
  const [modelDraftMatchesActive, setModelDraftMatchesActive] = useState(false);
  const sourceReady = onboarding.project !== undefined;
  const modelReady = onboarding.activeModel?.capability === "ready";
  const trialEnabled = sourceReady
    && modelReady
    && modelDraftMatchesActive
    && onboarding.readiness.trial
    && busyAction === undefined;
  const trialRunning = busyAction === "start-trial" || busyAction === "cancel-trial";
  const errorPanel = operationError === undefined ? null : (
    <section className="operation-error" role="status">
      <p>{redactTechnicalDetails(operationError.message)}</p>
      {operationError.nextAction === undefined ? null : <p>{redactTechnicalDetails(operationError.nextAction)}</p>}
      <TechnicalDetails details={operationError.technicalDetails} />
    </section>
  );

  if (pendingEncoding !== undefined) {
    return (
      <main className="onboarding-scroll">
        <div className="content-column welcome-page">
          <div className="welcome-grid">
            <section className="welcome-copy">
              <p className="eyebrow">FolioLoom / Encoding</p>
              <h1>确认书稿编码</h1>
              <p className="onboarding-lead">选对文字编码后再打开书稿，避免把乱码带进翻译。</p>
            </section>
            <EncodingChooser
              pending={pendingEncoding}
              busy={busyAction !== undefined}
              onConfirm={onConfirmSourceEncoding}
              onChooseAnother={onChooseSource}
            />
          </div>
          {errorPanel}
        </div>
      </main>
    );
  }

  if (!sourceReady) {
    return (
      <main className="onboarding-scroll">
        <div className="content-column welcome-page">
          <div className="welcome-grid">
            <section className="welcome-copy">
              <p className="eyebrow">FolioLoom / Start</p>
              <h1>开始翻译一本书</h1>
              <p className="onboarding-lead">选择书稿，连接你自己的模型，然后先用一个片段确认翻译效果。</p>
              <button
                className="primary-button"
                type="button"
                disabled={busyAction !== undefined}
                onClick={() => { void onChooseSource(); }}
              >
                {busyAction === "choose-source" ? "正在选择…" : "选择书稿"}
              </button>
            </section>
            <aside className="welcome-card">
              <p className="eyebrow">支持格式</p>
              <h2>直接选择原稿</h2>
              <p>程序会复制原文件用于翻译，不会改动你选择的书稿。</p>
              <div className="format-list" aria-label="支持的书稿格式">
                <span>TXT</span><span>EPUB</span><span>DOCX</span><span>Markdown</span>
              </div>
            </aside>
          </div>
          {errorPanel}
        </div>
      </main>
    );
  }

  return (
    <main className="onboarding-scroll">
      <div className="content-column setup-page">
        <header className="project-header">
          <div>
            <p className="eyebrow">项目概览</p>
            <h1>{onboarding.project?.title}</h1>
            <p className="section-copy">{onboarding.project?.sourceLanguage} · {formatChars(onboarding.project?.sourceChars ?? 0)} 字符</p>
          </div>
          <button
            className="quiet-button"
            type="button"
            disabled={busyAction !== undefined}
            onClick={() => { void onChooseSource(); }}
          >
            更换书稿
          </button>
        </header>

        <div className="setup-workspace-grid">
          <section className="setup-card source-card">
            <div className="card-status is-ready" aria-hidden="true">✓</div>
            <p className="eyebrow">书稿</p>
            <h2>原文已准备</h2>
            <p className="section-copy">支持 TXT、EPUB、DOCX 和 Markdown；原文件保持不变。</p>
          </section>

          <section className={`setup-card model-card${modelReady ? " is-ready" : ""}`} aria-labelledby="model-step-title">
            <div className={`card-status${modelReady ? " is-ready" : ""}`} aria-hidden="true">{modelReady ? "✓" : "2"}</div>
            <p className="eyebrow">模型</p>
            <h2 id="model-step-title">连接你的模型</h2>
            <p className="section-copy">选择服务，填写 API Key、模型和思考强度，再测试实际连接。</p>
            <ProviderSetup
              providers={onboarding.providers}
              activeModel={onboarding.activeModel}
              busy={busyAction !== undefined}
              onDiscoverModels={onDiscoverModels}
              onTestModel={onTestModel}
              onForgetCredential={onForgetCredential}
              onDraftValidityChange={setModelDraftMatchesActive}
            />
          </section>
        </div>

        <section className={`setup-card trial-card${trialResult !== undefined ? " is-ready" : ""}`} aria-labelledby="trial-step-title">
          <div className={`card-status${trialResult !== undefined ? " is-ready" : ""}`} aria-hidden="true">{trialResult !== undefined ? "✓" : "3"}</div>
          <div>
            <p className="eyebrow">试译</p>
            <h2 id="trial-step-title">先试译一小段</h2>
            <p className="section-copy">连接检查通过后，用一个片段确认模型能完整走通翻译流程。</p>
            {modelReady && !modelDraftMatchesActive ? (
              <p className="inline-note">当前设置尚未测试，重新测试连接后才能试译。</p>
            ) : null}
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

        {errorPanel}
      </div>
    </main>
  );
}
