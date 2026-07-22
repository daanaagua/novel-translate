import type { JSX } from "react";

import type { DesktopDoctorReport, DesktopResult } from "../../../contracts.js";

interface DoctorPanelProps {
  result: DesktopResult<DesktopDoctorReport> | undefined;
  busy: boolean;
  onRun(): void;
  onChooseProject(): void;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function recoveryAction(code: string): string | undefined {
  switch (code) {
    case "CANONICAL_HASH_MISMATCH":
      return "请恢复与 manifest 匹配的 canonical 原文后重试";
    case "RAW_HASH_MISMATCH":
      return "请检查与 manifest 匹配的原始文件后重试";
    case "MANIFEST_INVALID":
      return "请检查 source_manifest.json 的内容后重试";
    default:
      return undefined;
  }
}

function ReportDetails({ report }: { report: DesktopDoctorReport }): JSX.Element {
  return (
    <>
      <dl className="doctor-metrics">
        <div>
          <dt>原文覆盖</dt>
          <dd>已覆盖 {formatNumber(report.coveredChars)} / {formatNumber(report.sourceChars)} 字</dd>
        </div>
        <div>
          <dt>结构块</dt>
          <dd>{formatNumber(report.blockCount)} 个</dd>
        </div>
        <div>
          <dt>逻辑窗口</dt>
          <dd>{formatNumber(report.windowCount)} 个</dd>
        </div>
        <div>
          <dt>标注</dt>
          <dd>{formatNumber(report.annotationCount)} 条</dd>
        </div>
      </dl>

      <div className="doctor-notes">
        <p><span>异常</span>{report.anomalyCount === 0 ? "未发现" : `${report.anomalyCount} 项`}</p>
        <p><span>检查代码</span>{report.incidentCodes.length === 0 ? "无" : report.incidentCodes.join("、")}</p>
        {report.glossary === undefined ? null : (
          <p>
            <span>术语表</span>
            {report.glossary.totalTerms} 条，已匹配 {report.glossary.matchedTerms} 条，未匹配 {report.glossary.unmatchedTerms} 条
          </p>
        )}
        {report.glossary === undefined ? null : <p className="path-value">{report.glossary.path}</p>}
      </div>
    </>
  );
}

export function DoctorPanel({ result, busy, onRun, onChooseProject }: DoctorPanelProps): JSX.Element {
  if (result !== undefined && result.ok) {
    return (
      <section className="doctor-panel status-card" aria-labelledby="doctor-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">只读校验</p>
            <h2 id="doctor-heading">原文检查已完成</h2>
          </div>
          <button className="quiet-button" type="button" onClick={onRun} disabled={busy}>重新检查</button>
        </div>
        <ReportDetails report={result.value} />
      </section>
    );
  }

  if (result !== undefined) {
    const nextStep = recoveryAction(result.error.code);
    return (
      <section className="doctor-panel status-card is-error" aria-labelledby="doctor-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">只读校验</p>
            <h2 id="doctor-heading">检查未能完成</h2>
          </div>
          <code className="error-code">{result.error.code}</code>
        </div>
        <p className="doctor-error-message">{result.error.message}</p>
        <p className="doctor-recovery">{nextStep ?? "请重新选择项目，然后再次运行检查"}</p>
        {nextStep === undefined ? (
          <button className="quiet-button" type="button" onClick={onChooseProject}>重新选择项目</button>
        ) : (
          <button className="quiet-button" type="button" onClick={onRun} disabled={busy}>再次检查</button>
        )}
      </section>
    );
  }

  return (
    <section className="doctor-panel status-card" aria-labelledby="doctor-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">只读校验</p>
          <h2 id="doctor-heading">还没有运行检查</h2>
        </div>
        <button className="quiet-button" type="button" onClick={onRun} disabled={busy}>运行检查</button>
      </div>
      <p className="section-copy">检查原文覆盖、结构块、逻辑窗口与可选术语表；不会调用模型，也不会改写项目文件。</p>
    </section>
  );
}
