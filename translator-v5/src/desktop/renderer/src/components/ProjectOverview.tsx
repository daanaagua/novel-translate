import type { JSX } from "react";

import type {
  DesktopDoctorReport,
  DesktopProjectSnapshot,
  DesktopResult,
  DesktopRunSummary,
} from "../../../contracts.js";
import type { BusyAction } from "../types.js";
import { DoctorPanel } from "./DoctorPanel.js";

interface ProjectOverviewProps {
  snapshot: DesktopProjectSnapshot | undefined;
  doctorResult: DesktopResult<DesktopDoctorReport> | undefined;
  busyAction: BusyAction;
  operationError: { code: string; message: string } | undefined;
  onChooseProject(): void;
  onChooseStore(): void;
  onRefresh(): void;
  onSelectRun(runId: string): void;
  onRunDoctor(): void;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function storeLabel(snapshot: DesktopProjectSnapshot): string {
  switch (snapshot.store.state) {
    case "ready":
      return "已连接（只读）";
    case "invalid":
      return "与当前原文不匹配";
    case "not_found":
      return "未找到状态库";
  }
}

function RunCounters({ run }: { run: DesktopRunSummary }): JSX.Element {
  return (
    <dl className="run-counters">
      <div>
        <dt>进度</dt>
        <dd>已完成 {run.progress.completedWindows} / {run.progress.totalWindows} 窗口</dd>
      </div>
      <div>
        <dt>待处理</dt>
        <dd>{run.progress.pendingWindows}</dd>
      </div>
      <div>
        <dt>需人工查看</dt>
        <dd>{run.progress.humanRequiredWindows}</dd>
      </div>
      <div>
        <dt>警告 / 失败</dt>
        <dd>{run.progress.warningWindows} / {run.progress.failedWindows}</dd>
      </div>
    </dl>
  );
}

function EmptyProject({ onChooseProject, busy }: { onChooseProject(): void; busy: boolean }): JSX.Element {
  return (
    <main className="content-column empty-project">
      <div className="empty-project-copy">
        <p className="eyebrow">FolioLoom / Alpha</p>
        <h1>尚未打开项目</h1>
        <p>选择已经由 V5 初始化的 <code>source_manifest.json</code>，在这里查看原文、状态库与检查结果。</p>
        <button className="primary-button" type="button" onClick={onChooseProject} disabled={busy}>打开项目</button>
      </div>
      <aside className="empty-project-note">
        <p className="eyebrow">当前边界</p>
        <p>这个工作台只读取既有项目。翻译运行、术语编辑和导出会在后续阶段接入。</p>
      </aside>
    </main>
  );
}

function SelectedRun({ snapshot }: { snapshot: DesktopProjectSnapshot }): JSX.Element {
  const run = snapshot.runs.find((candidate) => candidate.runId === snapshot.selectedRunId);
  if (run === undefined) {
    return <p className="section-copy">尚未开始翻译</p>;
  }

  return (
    <>
      <div className="run-heading">
        <div>
          <p className="eyebrow">当前运行</p>
          <h2>{run.modelId}</h2>
        </div>
        <span className="run-status">{run.status}</span>
      </div>
      <RunCounters run={run} />
    </>
  );
}

function RunState({
  snapshot,
  busy,
  onChooseStore,
  onSelectRun,
}: {
  snapshot: DesktopProjectSnapshot;
  busy: boolean;
  onChooseStore(): void;
  onSelectRun(runId: string): void;
}): JSX.Element {
  if (snapshot.store.state === "not_found") {
    return (
      <section className="run-state-card">
        <p className="eyebrow">翻译运行</p>
        <h2>尚未开始翻译</h2>
        <p className="section-copy">尚未连接 V5 状态库。选择已有 <code>book.db</code> 后，可以只读查看已有运行记录。</p>
        <button className="quiet-button" type="button" onClick={onChooseStore} disabled={busy}>选择状态库</button>
      </section>
    );
  }

  if (snapshot.store.state === "invalid") {
    return (
      <section className="run-state-card is-invalid">
        <p className="eyebrow">翻译运行</p>
        <h2>状态库需要重新确认</h2>
        <p className="section-copy">{snapshot.store.error?.message ?? "状态库与当前原文版本不匹配"}</p>
        <button className="quiet-button" type="button" onClick={onChooseStore} disabled={busy}>选择状态库</button>
      </section>
    );
  }

  if (snapshot.runSelection === "required") {
    return (
      <section className="run-state-card">
        <p className="eyebrow">翻译运行</p>
        <h2>请选择要查看的运行</h2>
        <p className="section-copy">这个状态库中有多个与当前原文匹配的运行记录。</p>
        <div className="run-selection-list">
          {snapshot.runs.map((run) => (
            <button
              className="run-choice"
              type="button"
              key={run.runId}
              disabled={busy}
              onClick={() => onSelectRun(run.runId)}
            >
              <span>{run.modelId}</span>
              <small>{run.runId}</small>
            </button>
          ))}
        </div>
      </section>
    );
  }

  if (snapshot.runs.length === 0) {
    return (
      <section className="run-state-card">
        <p className="eyebrow">翻译运行</p>
        <h2>尚未开始翻译</h2>
        <p className="section-copy">状态库已连接，但其中还没有与当前原文匹配的翻译运行。</p>
      </section>
    );
  }

  return (
    <section className="run-state-card">
      <SelectedRun snapshot={snapshot} />
    </section>
  );
}

export function ProjectOverview({
  snapshot,
  doctorResult,
  busyAction,
  operationError,
  onChooseProject,
  onChooseStore,
  onRefresh,
  onSelectRun,
  onRunDoctor,
}: ProjectOverviewProps): JSX.Element {
  if (snapshot === undefined) {
    return <EmptyProject onChooseProject={onChooseProject} busy={busyAction === "choose-project"} />;
  }

  const busy = busyAction !== undefined;
  return (
    <main className="content-column">
      <header className="project-header">
        <div>
          <p className="eyebrow">项目概览</p>
          <h1>翻译中</h1>
          <p className="book-title">{snapshot.title}</p>
        </div>
        <button className="quiet-button" type="button" onClick={onRefresh} disabled={busy}>刷新状态</button>
      </header>

      {operationError === undefined ? null : (
        <div className="operation-error" role="status">
          <code>{operationError.code}</code>
          <span>{operationError.message}</span>
        </div>
      )}

      <section className="metadata-card" aria-label="项目元数据">
        <dl className="metadata">
          <div>
            <dt>源语言</dt>
            <dd>{snapshot.sourceLanguage}</dd>
          </div>
          <div>
            <dt>原文长度</dt>
            <dd>{formatNumber(snapshot.sourceChars)} 字</dd>
          </div>
          <div>
            <dt>状态库</dt>
            <dd>{storeLabel(snapshot)}</dd>
          </div>
          <div>
            <dt>源版本</dt>
            <dd className="source-version">{snapshot.sourceVersion}</dd>
          </div>
        </dl>
      </section>

      <div className="overview-grid">
        <RunState
          snapshot={snapshot}
          busy={busy}
          onChooseStore={onChooseStore}
          onSelectRun={onSelectRun}
        />
        <DoctorPanel
          result={doctorResult}
          busy={busyAction === "doctor"}
          onRun={onRunDoctor}
          onChooseProject={onChooseProject}
        />
      </div>
    </main>
  );
}
