import type { JSX } from "react";

import { WORKSPACES, type WorkspaceId } from "../types.js";

interface SidebarProps {
  activeWorkspace: WorkspaceId;
  hasProject: boolean;
  onSelectWorkspace(workspace: WorkspaceId): void;
}

export function Sidebar({
  activeWorkspace,
  hasProject,
  onSelectWorkspace,
}: SidebarProps): JSX.Element {
  return (
    <aside className="sidebar">
      <div className="brand-lockup">
        <img className="brand-mark" src="./folioloom-mark.svg" alt="" />
        <div>
          <p className="brand-name">FolioLoom</p>
          <p className="brand-subtitle">本地文稿工作台</p>
        </div>
      </div>

      <nav className="workspace-nav" aria-label="工作区">
        <p className="nav-label">工作区</p>
        {WORKSPACES.map((workspace, index) => {
          const selected = workspace.id === activeWorkspace;
          return (
            <button
              className={`nav-item${selected ? " is-active" : ""}`}
              type="button"
              key={workspace.id}
              aria-label={workspace.label}
              aria-current={selected ? "page" : undefined}
              onClick={() => onSelectWorkspace(workspace.id)}
            >
              <span className="nav-index" aria-hidden="true">0{index + 1}</span>
              <span>
                <span className="nav-name">{workspace.label}</span>
                <span className="nav-detail">{workspace.detail}</span>
              </span>
            </button>
          );
        })}
      </nav>

      <div className="sidebar-footnote">
        <span className={`connection-dot${hasProject ? " is-ready" : ""}`} aria-hidden="true" />
        <span>{hasProject ? "已打开本地项目" : "等待打开项目"}</span>
      </div>
    </aside>
  );
}
