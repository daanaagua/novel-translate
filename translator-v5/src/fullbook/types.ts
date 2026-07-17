export type BookWindowStatus =
  | "pending"
  | "running"
  | "completed"
  | "completed_with_warnings"
  | "human_required"
  | "failed";

export interface BookWindowPlan {
  windowId: string;
  ordinal: number;
  chapterId: string;
  chapterTitle: string | null;
  blockIds: string[];
  globalIndexes: number[];
  sourceTokens: number;
  sourceChars: number;
  oversized: boolean;
}

export interface WindowExecutionSummary {
  status: Extract<
    BookWindowStatus,
    "completed" | "completed_with_warnings" | "human_required" | "failed"
  >;
  modelCalls: number;
  modelCallLimit: number;
  repaired: boolean;
  deadlineExceeded: boolean;
}

export interface AdaptiveConcurrencyOptions {
  warmupWindows: number;
  maxConcurrency: number;
  budgetRiskRatio?: number;
}
