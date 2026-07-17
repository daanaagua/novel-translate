import { createHash } from "node:crypto";
import { resolve } from "node:path";

import type { StableTerm, V4Block } from "../domain/types.js";
import { EvidenceIndex } from "../index/evidence-index.js";
import { V4ReadAdapter } from "../storage/v4-read-adapter.js";

export class BookContext {
  readonly sourceDbPath: string;
  readonly sourceFingerprint: string;
  readonly #adapter: V4ReadAdapter;
  readonly #blocks: V4Block[];
  readonly #stableTerms: StableTerm[];
  readonly evidenceIndex: EvidenceIndex;
  #closed = false;

  private constructor(databasePath: string) {
    this.sourceDbPath = resolve(databasePath);
    this.#adapter = new V4ReadAdapter(this.sourceDbPath);
    this.#blocks = this.#adapter.loadBlocks();
    if (this.#blocks.length === 0) {
      this.#adapter.close();
      throw new Error("source database contains no blocks");
    }
    this.#stableTerms = this.#adapter.loadStableTerms();
    this.evidenceIndex = EvidenceIndex.fromBlocks(this.#blocks);
    const digest = createHash("sha256");
    for (const block of this.#blocks) {
      digest.update(block.id);
      digest.update("\0");
      digest.update(block.sourceHash);
      digest.update("\0");
      digest.update(String(block.globalIndex));
      digest.update("\n");
    }
    this.sourceFingerprint = digest.digest("hex");
  }

  static open(databasePath: string): BookContext {
    return new BookContext(databasePath);
  }

  get blocks(): V4Block[] {
    this.#assertOpen();
    return this.#blocks.map((block) => ({ ...block }));
  }

  get stableTerms(): StableTerm[] {
    this.#assertOpen();
    return this.#stableTerms.map((term) => ({ ...term }));
  }

  blocksForIndexes(globalIndexes: readonly number[]): V4Block[] {
    this.#assertOpen();
    const requested = new Set(globalIndexes);
    return this.#blocks
      .filter((block) => requested.has(block.globalIndex))
      .map((block) => ({ ...block }));
  }

  close(): void {
    if (this.#closed) {
      return;
    }
    this.#closed = true;
    this.evidenceIndex.close();
    this.#adapter.close();
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new Error("book context is closed");
    }
  }
}
