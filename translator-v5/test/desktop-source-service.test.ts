import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import test from "node:test";

import { DesktopSourceService } from "../src/desktop/desktop-source-service.js";
import { SourceLedger } from "../src/source/source-ledger.js";

function fixtureDirectory(): string {
  return mkdtempSync(join(tmpdir(), "folioloom-desktop-source-service-"));
}

test("desktop source service creates a reader-named project and never rewrites the selected manuscript", async () => {
  const directory = fixtureDirectory();
  const sourcePath = join(directory, "My: Novel?.txt");
  const projectsRoot = join(directory, "Projects");
  const source = Buffer.from(
    "The book is in the house, and the garden is on the hill. We return to the house with a book and a map.",
    "utf8",
  );
  writeFileSync(sourcePath, source);
  try {
    const service = new DesktopSourceService({ projectsRoot });
    const imported = await service.importSource({ sourcePath, sourceLanguage: "auto" });
    const ledger = SourceLedger.open(imported.manifestPath);

    assert.equal(imported.reused, false);
    assert.equal(ledger.sourceText, source.toString("utf8"));
    assert.equal(ledger.sourceLanguage, "en");
    assert.equal(existsSync(join(imported.projectDirectory, "source", "original.txt")), true);
    assert.equal(readFileEquals(sourcePath, source), true);
    assert.match(basename(imported.projectDirectory), /^My-Novel-[a-f0-9]{12}$/u);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("desktop source service restores a project when the raw source hash was imported before", async () => {
  const directory = fixtureDirectory();
  const projectsRoot = join(directory, "Projects");
  const firstPath = join(directory, "First Title.txt");
  const renamedPath = join(directory, "Renamed Title.txt");
  writeFileSync(firstPath, "Identical source bytes.", "utf8");
  writeFileSync(renamedPath, "Identical source bytes.", "utf8");
  try {
    const service = new DesktopSourceService({ projectsRoot });
    const first = await service.importSource({ sourcePath: firstPath, sourceLanguage: "en" });
    const second = await service.importSource({ sourcePath: renamedPath, sourceLanguage: "en" });

    assert.equal(first.reused, false);
    assert.equal(second.reused, true);
    assert.equal(second.projectDirectory, first.projectDirectory);
    assert.equal(second.manifestPath, first.manifestPath);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("desktop source service keeps same-title manuscripts with different bytes as independent projects", async () => {
  const directory = fixtureDirectory();
  const projectsRoot = join(directory, "Projects");
  const firstDirectory = join(directory, "one");
  const secondDirectory = join(directory, "two");
  const firstPath = join(firstDirectory, "Same Title.txt");
  const secondPath = join(secondDirectory, "Same Title.txt");
  mkdirSync(firstDirectory);
  mkdirSync(secondDirectory);
  writeFileSync(firstPath, "First manuscript.", "utf8");
  writeFileSync(secondPath, "Second manuscript.", "utf8");
  try {
    const service = new DesktopSourceService({ projectsRoot });
    const first = await service.importSource({ sourcePath: firstPath, sourceLanguage: "en" });
    const second = await service.importSource({ sourcePath: secondPath, sourceLanguage: "en" });

    assert.equal(first.reused, false);
    assert.equal(second.reused, false);
    assert.notEqual(first.projectDirectory, second.projectDirectory);
    assert.match(basename(first.projectDirectory), /^Same-Title-[a-f0-9]{12}$/u);
    assert.match(basename(second.projectDirectory), /^Same-Title-[a-f0-9]{12}$/u);
    assert.equal(dirname(first.manifestPath), first.projectDirectory);
    assert.equal(dirname(second.manifestPath), second.projectDirectory);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

function readFileEquals(path: string, expected: Buffer): boolean {
  return readFileSync(path).equals(expected);
}
