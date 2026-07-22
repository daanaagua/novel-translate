import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { deflateRawSync } from "node:zlib";
import test from "node:test";

import { SourceLedger } from "../src/source/source-ledger.js";
import {
  SourceImportError,
  SourceImporter,
  importSource,
} from "../src/source/source-importer.js";
import { scalarLength } from "../src/source/types.js";

interface ZipEntry {
  name: string;
  data: string | Buffer;
  method?: 0 | 8;
  declaredUncompressedSize?: number;
}

function crc32(input: Buffer): number {
  let crc = 0xffffffff;
  for (const byte of input) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function u16(value: number): Buffer {
  const output = Buffer.alloc(2);
  output.writeUInt16LE(value, 0);
  return output;
}

function u32(value: number): Buffer {
  const output = Buffer.alloc(4);
  output.writeUInt32LE(value >>> 0, 0);
  return output;
}

/** Makes the tiny ZIP fixtures in this test self-contained, with no writer dependency. */
function zip(entries: readonly ZipEntry[]): Buffer {
  const localRecords: Buffer[] = [];
  const centralRecords: Buffer[] = [];
  let offset = 0;
  for (const entry of entries) {
    const name = Buffer.from(entry.name, "utf8");
    const uncompressed = Buffer.isBuffer(entry.data) ? entry.data : Buffer.from(entry.data, "utf8");
    const method = entry.method ?? 0;
    const payload = method === 8 ? deflateRawSync(uncompressed) : uncompressed;
    const declaredSize = entry.declaredUncompressedSize ?? uncompressed.length;
    const checksum = crc32(uncompressed);
    const local = Buffer.concat([
      u32(0x04034b50), u16(20), u16(0x0800), u16(method), u16(0), u16(0),
      u32(checksum), u32(payload.length), u32(declaredSize), u16(name.length), u16(0), name, payload,
    ]);
    localRecords.push(local);
    centralRecords.push(Buffer.concat([
      u32(0x02014b50), u16(20), u16(20), u16(0x0800), u16(method), u16(0), u16(0),
      u32(checksum), u32(payload.length), u32(declaredSize), u16(name.length), u16(0), u16(0),
      u16(0), u16(0), u32(0), u32(offset), name,
    ]));
    offset += local.length;
  }
  const central = Buffer.concat(centralRecords);
  return Buffer.concat([
    ...localRecords,
    central,
    u32(0x06054b50), u16(0), u16(0), u16(entries.length), u16(entries.length),
    u32(central.length), u32(offset), u16(0),
  ]);
}

function writeFixture(name: string, payload: Buffer): { directory: string; sourcePath: string } {
  const directory = mkdtempSync(join(tmpdir(), "folioloom-source-import-"));
  const sourcePath = join(directory, name);
  writeFileSync(sourcePath, payload);
  return { directory, sourcePath };
}

function projectDirectory(parent: string, name = "project"): string {
  return join(parent, name);
}

function expectImportCode(action: () => Promise<unknown>, code: string): Promise<void> {
  return assert.rejects(action, (error: unknown) => (
    error instanceof SourceImportError && error.code === code
  ));
}

function utf16be(value: string): Buffer {
  const little = Buffer.from(value, "utf16le");
  for (let index = 0; index < little.length; index += 2) {
    const first = little[index]!;
    little[index] = little[index + 1]!;
    little[index + 1] = first;
  }
  return little;
}

function utf32(value: string, littleEndian: boolean): Buffer {
  const parts: Buffer[] = [];
  for (const scalar of value) {
    const buffer = Buffer.alloc(4);
    if (littleEndian) {
      buffer.writeUInt32LE(scalar.codePointAt(0)!);
    } else {
      buffer.writeUInt32BE(scalar.codePointAt(0)!);
    }
    parts.push(buffer);
  }
  return Buffer.concat(parts);
}

function bomEncoded(name: string, source: string): { payload: Buffer; encoding: string; bomSize: number } {
  switch (name) {
    case "utf8":
      return { payload: Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(source)]), encoding: "utf-8", bomSize: 3 };
    case "utf16le":
      return { payload: Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(source, "utf16le")]), encoding: "utf-16le", bomSize: 2 };
    case "utf16be":
      return { payload: Buffer.concat([Buffer.from([0xfe, 0xff]), utf16be(source)]), encoding: "utf-16be", bomSize: 2 };
    case "utf32le":
      return { payload: Buffer.concat([Buffer.from([0xff, 0xfe, 0x00, 0x00]), utf32(source, true)]), encoding: "utf-32le", bomSize: 4 };
    case "utf32be":
      return { payload: Buffer.concat([Buffer.from([0x00, 0x00, 0xfe, 0xff]), utf32(source, false)]), encoding: "utf-32be", bomSize: 4 };
    default:
      throw new TypeError(`unsupported BOM fixture ${name}`);
  }
}

test("source importer writes a certified scalar ledger for UTF-8 text and Markdown", async () => {
  const text = writeFixture("novel.txt", Buffer.from("Alpha\r\n😀Beta", "utf8"));
  const markdown = writeFixture("notes.md", Buffer.from("# Heading\r\n\r\nBody", "utf8"));
  try {
    const textResult = await importSource({
      sourcePath: text.sourcePath,
      projectDirectory: projectDirectory(text.directory),
      sourceLanguage: "en",
    });
    const textLedger = SourceLedger.open(textResult.manifestPath);
    assert.equal(textLedger.sourceText, "Alpha\n😀Beta");
    assert.equal(textLedger.canonicalChars, scalarLength("Alpha\n😀Beta"));
    assert.deepEqual(textLedger.canonicalSegments.map((segment) => [
      segment.canonicalStart,
      segment.canonicalEnd,
      segment.originKind,
      segment.originRef,
    ]), [[0, scalarLength("Alpha\n😀Beta"), "decoded_bytes", "source/original.txt"]]);
    assert.equal(readFileSync(textLedger.rawPath).equals(readFileSync(text.sourcePath)), true);
    // This deliberately underspecified fragment must not be guessed as English.
    assert.equal(textResult.detectedLanguage, undefined);

    const markdownResult = await importSource({
      sourcePath: markdown.sourcePath,
      projectDirectory: projectDirectory(markdown.directory),
      sourceLanguage: "en",
    });
    const markdownManifest = JSON.parse(readFileSync(markdownResult.manifestPath, "utf8")) as Record<string, unknown>;
    assert.equal(markdownManifest.source_format, ".md");
    assert.equal(SourceLedger.open(markdownResult.manifestPath).sourceText, "# Heading\n\nBody");
  } finally {
    rmSync(text.directory, { recursive: true, force: true });
    rmSync(markdown.directory, { recursive: true, force: true });
  }
});

test("source importer emits an automatic language hint only with enough evidence", async () => {
  const fixture = writeFixture(
    "language.txt",
    Buffer.from(
      "The book is in the house, and the garden is on the hill. We return to the house with a book and a map.",
      "utf8",
    ),
  );
  try {
    const result = await importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: projectDirectory(fixture.directory),
      sourceLanguage: "auto",
    });
    assert.equal(result.detectedLanguage?.id, "en");
    assert.equal(SourceLedger.open(result.manifestPath).sourceLanguage, "en");
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("source importer accepts all five Unicode BOM forms without losing scalar coordinates", async () => {
  const source = "A😀\r\nB";
  for (const name of ["utf8", "utf16le", "utf16be", "utf32le", "utf32be"] as const) {
    const fixture = writeFixture(`book-${name}.txt`, bomEncoded(name, source).payload);
    try {
      const result = await importSource({
        sourcePath: fixture.sourcePath,
        projectDirectory: projectDirectory(fixture.directory),
        sourceLanguage: "en",
      });
      const ledger = SourceLedger.open(result.manifestPath);
      const manifest = JSON.parse(readFileSync(result.manifestPath, "utf8")) as Record<string, unknown>;
      const expected = bomEncoded(name, source);
      assert.equal(ledger.sourceText, "A😀\nB", name);
      assert.equal(manifest.encoding, expected.encoding, name);
      assert.deepEqual(ledger.canonicalSegments.map((segment) => [
        segment.canonicalStart,
        segment.canonicalEnd,
      ]), [[0, scalarLength("A😀\nB")]], name);
      assert.deepEqual(ledger.excludedRawRanges.map((range) => [range.rawStart, range.rawEnd]), [[0, expected.bomSize]], name);
    } finally {
      rmSync(fixture.directory, { recursive: true, force: true });
    }
  }
});

test("source importer refuses ambiguous non-BOM bytes instead of replacing them", async () => {
  const fixture = writeFixture("legacy.txt", Buffer.from([0x80, 0x81, 0x82]));
  const destination = projectDirectory(fixture.directory);
  try {
    await expectImportCode(() => importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: destination,
      sourceLanguage: "und",
    }), "ENCODING_AMBIGUOUS");
    assert.equal(existsSync(destination), false);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("source importer removes its temporary project if source bytes change before publication", async () => {
  const fixture = writeFixture("volatile.txt", Buffer.from("initial", "utf8"));
  const destination = projectDirectory(fixture.directory);
  let reads = 0;
  const importer = new SourceImporter({
    readSource: async () => {
      reads += 1;
      return Buffer.from(reads === 1 ? "initial" : "changed", "utf8");
    },
  });
  try {
    await expectImportCode(() => importer.importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: destination,
      sourceLanguage: "en",
    }), "SOURCE_CHANGED_DURING_IMPORT");
    assert.equal(existsSync(destination), false);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("source importer preserves DOCX paragraph order and empty paragraphs", async () => {
  const document = `<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="urn:test"><w:body>
      <w:p><w:r><w:t>First</w:t></w:r></w:p>
      <w:p/>
      <w:p><w:r><w:t>Third</w:t></w:r></w:p>
    </w:body></w:document>`;
  const fixture = writeFixture("book.docx", zip([{ name: "word/document.xml", data: document }]));
  try {
    const result = await importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: projectDirectory(fixture.directory),
      sourceLanguage: "en",
    });
    const ledger = SourceLedger.open(result.manifestPath);
    assert.equal(ledger.sourceText, "First\n\n\n\nThird");
    assert.deepEqual(ledger.canonicalSegments.map((segment) => segment.originKind), [
      "docx_paragraph", "docx_paragraph", "docx_paragraph",
    ]);
    assert.deepEqual(ledger.canonicalSegments.map((segment) => segment.originRef), [
      "word/document.xml#p=0", "word/document.xml#p=1", "word/document.xml#p=2",
    ]);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("source importer preserves DOCX run order through hyperlinks and table paragraphs", async () => {
  const document = `<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="urn:test"><w:body>
      <w:p><w:r><w:t>Alpha</w:t></w:r><w:hyperlink><w:r><w:t> Beta</w:t></w:r></w:hyperlink><w:r><w:tab/><w:t>Gamma</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell one</w:t></w:r><w:hyperlink><w:r><w:t> link</w:t></w:r></w:hyperlink></w:p></w:tc><w:tc><w:p><w:r><w:t>Cell two</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:body></w:document>`;
  const fixture = writeFixture("mixed.docx", zip([{ name: "word/document.xml", data: document }]));
  try {
    const result = await importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: projectDirectory(fixture.directory),
      sourceLanguage: "en",
    });
    const ledger = SourceLedger.open(result.manifestPath);
    assert.equal(ledger.sourceText, "Alpha Beta\tGamma\n\nCell one link\n\nCell two");
    assert.deepEqual(ledger.canonicalSegments.map((segment) => segment.originRef), [
      "word/document.xml#p=0", "word/document.xml#p=1", "word/document.xml#p=2",
    ]);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("source importer follows EPUB container, OPF manifest and spine order", async () => {
  const container = `<?xml version="1.0"?><container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>`;
  const opf = `<package><manifest>
      <item id="first" href="text/first.xhtml" media-type="application/xhtml+xml"/>
      <item id="second" href="text/second.xhtml" media-type="application/xhtml+xml"/>
    </manifest><spine><itemref idref="second"/><itemref idref="first"/></spine></package>`;
  const fixture = writeFixture("book.epub", zip([
    { name: "META-INF/container.xml", data: container },
    { name: "OEBPS/content.opf", data: opf },
    { name: "OEBPS/text/first.xhtml", data: "<html><body><p>First</p></body></html>" },
    { name: "OEBPS/text/second.xhtml", data: "<html><body><p>Second</p></body></html>" },
  ]));
  try {
    const result = await importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: projectDirectory(fixture.directory),
      sourceLanguage: "en",
    });
    const ledger = SourceLedger.open(result.manifestPath);
    assert.equal(ledger.sourceText, "Second\n\nFirst");
    assert.deepEqual(ledger.canonicalSegments.map((segment) => [segment.originKind, segment.originRef]), [
      ["epub_spine_member", "OEBPS/text/second.xhtml"],
      ["epub_spine_member", "OEBPS/text/first.xhtml"],
    ]);
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("source importer accepts a standard external XHTML doctype in EPUB spine documents", async () => {
  const container = `<?xml version="1.0"?><container><rootfiles><rootfile full-path="metadata.opf"/></rootfiles></container>`;
  const opf = `<package><manifest><item id="front" href="content/FrontPage.html" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="front"/></spine></package>`;
  const frontPage = `<?xml version="1.0" encoding="utf-8"?>
    <!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
    <html xmlns="http://www.w3.org/1999/xhtml"><body><div>Little, Big</div></body></html>`;
  const fixture = writeFixture("external-doctype.epub", zip([
    { name: "META-INF/container.xml", data: container },
    { name: "metadata.opf", data: opf },
    { name: "content/FrontPage.html", data: frontPage },
  ]));
  try {
    const result = await importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: projectDirectory(fixture.directory),
      sourceLanguage: "en",
    });
    assert.equal(SourceLedger.open(result.manifestPath).sourceText, "Little, Big");
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("source importer still rejects EPUB spine documents with an internal DTD subset", async () => {
  const container = `<container><rootfiles><rootfile full-path="metadata.opf"/></rootfiles></container>`;
  const opf = `<package><manifest><item id="chapter" href="chapter.xhtml"/></manifest><spine><itemref idref="chapter"/></spine></package>`;
  const chapter = `<!DOCTYPE html [<!ELEMENT html ANY>]><html><body>Unsafe</body></html>`;
  const fixture = writeFixture("internal-dtd.epub", zip([
    { name: "META-INF/container.xml", data: container },
    { name: "metadata.opf", data: opf },
    { name: "chapter.xhtml", data: chapter },
  ]));
  try {
    await expectImportCode(() => importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: projectDirectory(fixture.directory),
      sourceLanguage: "en",
    }), "EPUB_SPINE_INVALID");
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("source importer preserves EPUB inline order and nested visible blocks exactly once", async () => {
  const container = `<?xml version="1.0"?><container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>`;
  const opf = `<package><manifest><item id="chapter" href="text/chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>`;
  const chapter = `<html><body><p>Hello <em>beautiful</em> world</p><section><div>Nested <strong>block</strong></div></section><pre>Pre <span>formatted</span>\n line</pre><table><tr><td>Cell <b>one</b></td><td>Cell two</td></tr></table></body></html>`;
  const fixture = writeFixture("mixed.epub", zip([
    { name: "META-INF/container.xml", data: container },
    { name: "OEBPS/content.opf", data: opf },
    { name: "OEBPS/text/chapter.xhtml", data: chapter },
  ]));
  try {
    const result = await importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: projectDirectory(fixture.directory),
      sourceLanguage: "en",
    });
    const ledger = SourceLedger.open(result.manifestPath);
    assert.equal(
      ledger.sourceText,
      "Hello beautiful world\n\nNested block\n\nPre formatted\n line\n\nCell one\n\nCell two",
    );
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("source importer rejects dangerous or incomplete archive inputs with stable codes", async () => {
  const fixtures = [
    {
      name: "traversal.epub",
      archive: zip([{ name: "../escape.xhtml", data: "oops" }]),
      code: "ARCHIVE_ENTRY_INVALID",
    },
    {
      name: "large.docx",
      archive: zip([{ name: "word/document.xml", data: "<w:document/>", declaredUncompressedSize: 64 * 1024 * 1024 + 1 }]),
      code: "ARCHIVE_ENTRY_TOO_LARGE",
    },
    {
      name: "missing-document.docx",
      archive: zip([{ name: "word/styles.xml", data: "<styles/>" }]),
      code: "DOCX_DOCUMENT_MISSING",
    },
    {
      name: "missing-container.epub",
      archive: zip([{ name: "OEBPS/content.opf", data: "<package/>" }]),
      code: "EPUB_CONTAINER_MISSING",
    },
    {
      name: "missing-opf.epub",
      archive: zip([{ name: "META-INF/container.xml", data: "<container><rootfiles/></container>" }]),
      code: "EPUB_OPF_MISSING",
    },
  ];
  for (const item of fixtures) {
    const fixture = writeFixture(item.name, item.archive);
    const destination = projectDirectory(fixture.directory);
    try {
      await expectImportCode(() => importSource({
        sourcePath: fixture.sourcePath,
        projectDirectory: destination,
        sourceLanguage: "en",
      }), item.code);
      assert.equal(existsSync(destination), false, item.name);
    } finally {
      rmSync(fixture.directory, { recursive: true, force: true });
    }
  }
});

test("source importer rejects ZIP archives whose declared expanded total exceeds the limit", async () => {
  const fixture = writeFixture("total.epub", zip(Array.from({ length: 9 }, (_, index) => ({
    name: `entry-${index}.bin`,
    data: "x",
    declaredUncompressedSize: 60 * 1024 * 1024,
  }))));
  try {
    await expectImportCode(() => importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: projectDirectory(fixture.directory),
      sourceLanguage: "en",
    }), "ARCHIVE_TOO_LARGE");
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});

test("source importer retains a raw hash that matches the copied original bytes", async () => {
  const payload = Buffer.from("Raw bytes\r\nstay certified", "utf8");
  const fixture = writeFixture("hash.txt", payload);
  try {
    const result = await importSource({
      sourcePath: fixture.sourcePath,
      projectDirectory: projectDirectory(fixture.directory),
      sourceLanguage: "en",
    });
    assert.equal(result.rawSha256, createHash("sha256").update(payload).digest("hex"));
    assert.equal(basename(SourceLedger.open(result.manifestPath).rawPath), "original.txt");
  } finally {
    rmSync(fixture.directory, { recursive: true, force: true });
  }
});
