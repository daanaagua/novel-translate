export type AgentPhase = "research" | "translation" | "repair";

export type VisibilityChannel =
  | "narrative_before_target"
  | "translator_global";

export type RunStatus =
  | "created"
  | "indexed"
  | "researched"
  | "translating"
  | "validating"
  | "completed"
  | "completed_with_warnings"
  | "human_required"
  | "failed";
