import type { JSX } from "react";

import type { DesktopOnboardingState } from "../../../contracts.js";

interface SidebarProps {
  onboarding: DesktopOnboardingState;
}

interface StepProps {
  index: number;
  label: string;
  ready: boolean;
}

function Step({ index, label, ready }: StepProps): JSX.Element {
  return (
    <li className={`sidebar-step${ready ? " is-ready" : ""}`}>
      <span className="sidebar-step-index">{index}</span>
      <span>{label}</span>
    </li>
  );
}

export function Sidebar({ onboarding }: SidebarProps): JSX.Element {
  const sourceReady = onboarding.project !== undefined;
  const modelReady = onboarding.activeModel?.capability === "ready";
  return (
    <aside className="sidebar">
      <div className="brand-lockup">
        <img className="brand-mark" src="./folioloom-mark.svg" alt="" />
        <div>
          <p className="brand-name">FolioLoom</p>
          <p className="brand-subtitle">长篇翻译</p>
        </div>
      </div>

      <nav className="setup-nav" aria-label="准备步骤">
        <p className="nav-label">准备</p>
        <ol>
          <Step index={1} label="书稿" ready={sourceReady} />
          <Step index={2} label="模型" ready={modelReady} />
          <Step index={3} label="试译" ready={sourceReady && modelReady} />
        </ol>
      </nav>

      <div className="sidebar-footnote">
        <span className={`connection-dot${sourceReady ? " is-ready" : ""}`} aria-hidden="true" />
        <span>{sourceReady ? "书稿已就绪" : "等待书稿"}</span>
      </div>
    </aside>
  );
}
