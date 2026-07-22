import type { JSX } from "react";

import type { DesktopProjectSnapshot } from "../../../contracts.js";
import { WORKSPACES, type WorkspaceId } from "../types.js";

interface WorkspacePlaceholderProps {
  workspace: Exclude<WorkspaceId, "overview">;
  snapshot: DesktopProjectSnapshot | undefined;
  onChooseProject(): void;
}

const workspaceCopy: Record<Exclude<WorkspaceId, "overview">, string> = {
  runs: "在这里集中查看和控制翻译运行。当前 Alpha 仅在项目概览中以只读方式展示已有运行记录。",
  memory: "在这里检查术语、实体和按位置开放的叙事记忆。当前 Alpha 不编辑知识库。",
  review: "在这里处理人工确认、风险提示和需要回看的文本块。当前 Alpha 不会创建审阅任务。",
  export: "在这里组织 TXT、EPUB 和其他交付文件。当前 Alpha 不生成或写出译文。",
};

export function WorkspacePlaceholder({
  workspace,
  snapshot,
  onChooseProject,
}: WorkspacePlaceholderProps): JSX.Element {
  const label = WORKSPACES.find((candidate) => candidate.id === workspace)?.label ?? "工作区";
  if (snapshot === undefined) {
    return (
      <main className="content-column placeholder-page">
        <p className="eyebrow">{label}</p>
        <h1>尚未打开项目</h1>
        <p className="section-copy">请先打开已经初始化的项目，再查看这个工作区的只读信息。</p>
        <button className="primary-button" type="button" onClick={onChooseProject}>打开项目</button>
      </main>
    );
  }

  return (
    <main className="content-column placeholder-page">
      <p className="eyebrow">{label}</p>
      <h1>{label}</h1>
      <p className="placeholder-lead">{workspaceCopy[workspace]}</p>
      <section className="placeholder-boundary">
        <p className="eyebrow">当前阶段</p>
        <h2>将在运行控制阶段接入</h2>
        <p>已连接项目：{snapshot.title}。目前这里不提供写入、启动或导出操作。</p>
      </section>
    </main>
  );
}
