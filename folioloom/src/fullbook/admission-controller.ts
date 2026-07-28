import type { SchedulerMode } from "./optimization-policy.js";
import type {
  LedgerBaselineSource,
  LedgerEvent,
  LedgerPurpose,
  LedgerReleaseReason,
  LedgerSettleOutcome,
  TokenLedger,
} from "./token-ledger.js";

export class BookTokenEnvelopeExceededError extends Error {
  readonly code = "TOKEN_ENVELOPE_EXHAUSTED" as const;
  readonly retryable = false;

  constructor(
    readonly actualTokens: number,
    readonly runningReservedTokens: number,
    readonly minimumPendingTokens: number,
    readonly allowedTokens: number,
  ) {
    super(
      `TOKEN_ENVELOPE_EXHAUSTED: actual ${actualTokens} + running `
      + `${runningReservedTokens} + minimum pending ${minimumPendingTokens} `
      + `exceeds allowed ${allowedTokens}`,
    );
    this.name = "BookTokenEnvelopeExceededError";
  }
}

export interface AdmissionControllerOptions {
  readonly ledger: TokenLedger;
  readonly mode: SchedulerMode;
  readonly persist: (event: LedgerEvent) => void;
}

export interface AdmissionReserveRequest {
  readonly requestId: string;
  readonly purpose: LedgerPurpose;
  readonly taskIds: readonly string[];
  readonly predictedTokens: number;
  readonly attempt: number;
}

export interface AdmissionSettleRequest {
  readonly requestId: string;
  readonly actualTokens: number;
  readonly usageComplete: boolean;
  readonly outcome: LedgerSettleOutcome;
}

export interface AdmissionBaselineRequest {
  readonly taskIds: readonly string[];
  readonly baselineTokens: number;
  readonly source: LedgerBaselineSource;
  readonly reason: string;
}

/**
 * Single write path for run-level token envelope decisions.
 * Planner remains pure; this controller is the only place that should
 * reserve/settle/release against the durable ledger during execution.
 */
export class AdmissionController {
  readonly #ledger: TokenLedger;
  readonly #mode: SchedulerMode;
  readonly #persist: (event: LedgerEvent) => void;

  constructor(options: AdmissionControllerOptions) {
    this.#ledger = options.ledger;
    this.#mode = options.mode;
    this.#persist = options.persist;
  }

  get ledger(): TokenLedger {
    return this.#ledger;
  }

  spentTokens(): number {
    return this.#ledger.state().spentTokens;
  }

  reservedTokens(): number {
    return this.#ledger.state().reservedTokens;
  }

  allowedTokens(): number {
    return this.#ledger.state().allowedTokens;
  }

  canLaunch(
    predictedTokens: number,
    conservativeHorizonFloor = 0,
  ): boolean {
    if (this.#mode !== "active") return true;
    return this.#ledger.canReserve(predictedTokens, conservativeHorizonFloor);
  }

  addBaseline(request: AdmissionBaselineRequest): void {
    const fresh = request.taskIds.filter(
      (taskId) => !this.#ledger.state().baselinedTaskIds.has(taskId),
    );
    if (fresh.length === 0 || request.baselineTokens <= 0) return;
    this.#apply({
      type: "baseline_added",
      taskIds: fresh,
      baselineTokens: request.baselineTokens,
      source: request.source,
      reason: request.reason,
    });
  }

  reserve(request: AdmissionReserveRequest): void {
    if (this.#ledger.state().openReservations.has(request.requestId)) {
      return;
    }
    const predictedTokens = Math.max(1, request.predictedTokens);
    if (this.#mode === "active" && !this.#ledger.canReserve(predictedTokens, 0)) {
      throw new BookTokenEnvelopeExceededError(
        this.spentTokens(),
        this.reservedTokens(),
        predictedTokens,
        this.allowedTokens(),
      );
    }
    this.#apply({
      type: "reserved",
      requestId: request.requestId,
      purpose: request.purpose,
      taskIds: request.taskIds,
      predictedTokens,
      attempt: request.attempt,
    });
  }

  settle(request: AdmissionSettleRequest): void {
    if (!this.#ledger.state().openReservations.has(request.requestId)) {
      return;
    }
    this.#apply({
      type: "settled",
      requestId: request.requestId,
      actualTokens: request.actualTokens,
      usageComplete: request.usageComplete,
      outcome: request.outcome,
    });
  }

  release(requestId: string, reason: LedgerReleaseReason): void {
    if (!this.#ledger.state().openReservations.has(requestId)) {
      return;
    }
    this.#apply({
      type: "released",
      requestId,
      reason,
    });
  }

  /**
   * Temporary secondary headroom (protocol/split). Caller must release;
   * actual usage settles on the parent request.
   */
  holdSecondary(request: AdmissionReserveRequest): { release(): void } {
    this.reserve(request);
    let released = false;
    return {
      release: () => {
        if (released) return;
        released = true;
        this.release(request.requestId, "superseded");
      },
    };
  }

  #apply(event: LedgerEvent): void {
    this.#persist(event);
  }
}
