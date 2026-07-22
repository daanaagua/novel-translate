import type { JSX } from "react";

import type { DesktopProjectSnapshot } from "../../../contracts.js";
import type { WorkspaceId } from "../types.js";

interface WorkspacePlaceholderProps {
  workspace: Exclude<WorkspaceId, "overview">;
  snapshot: DesktopProjectSnapshot | undefined;
  onChooseProject(): void;
}

export function WorkspacePlaceholder({ snapshot, onChooseProject }: WorkspacePlaceholderProps): JSX.Element {
  return (
    <main className="onboarding-scroll">
      <div className="onboarding-column">
        <p className="eyebrow">FolioLoom</p>
        <h1>{snapshot?.title ?? "先选择书稿"}</h1>
        <p className="section-copy">此处会在后续翻译流程中开放。</p>
        <button className="primary-button" type="button" onClick={onChooseProject}>选择书稿</button>
      </div>
    </main>
  );
}
