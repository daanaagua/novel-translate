export type BusyAction =
  | "choose-source"
  | "discover-models"
  | "test-model"
  | "forget-credential"
  | "start-trial"
  | "cancel-trial"
  | "choose-project"
  | "choose-store"
  | "refresh"
  | "select-run"
  | "doctor"
  | undefined;

// Retained while the later workspace views are still being connected.
export const WORKSPACES = [
  { id: "overview", label: "准备", detail: "选择书稿和模型" },
  { id: "runs", label: "翻译", detail: "即将接入" },
  { id: "memory", label: "记忆", detail: "即将接入" },
  { id: "review", label: "审阅", detail: "即将接入" },
  { id: "export", label: "导出", detail: "即将接入" },
] as const;

export type WorkspaceId = (typeof WORKSPACES)[number]["id"];
