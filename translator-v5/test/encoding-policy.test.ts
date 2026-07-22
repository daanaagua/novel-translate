import assert from "node:assert/strict";
import test from "node:test";

import {
  EncodingPolicyError,
  decodeSourceBytes,
  normalizeEncodingLabel,
} from "../src/source/encoding-policy.js";

const SHIFT_JIS_JAPANESE = Buffer.from([
  0x93, 0xfa, 0x96, 0x7b, 0x8c, 0xea,
  0x82, 0xc5, 0x82, 0xb7, 0x81, 0x42,
]);
const EUC_JP_JAPANESE = Buffer.from([
  0xc6, 0xfc, 0xcb, 0xdc, 0xb8, 0xec,
  0xa4, 0xc7, 0xa4, 0xb9, 0xa1, 0xa3,
]);
const EUC_KR_KOREAN = Buffer.from([
  0xc7, 0xd1, 0xb1, 0xb9, 0xbe, 0xee,
]);

test("encoding labels normalize user-facing Japanese and Korean aliases", () => {
  assert.equal(normalizeEncodingLabel("UTF8"), "utf-8");
  assert.equal(normalizeEncodingLabel("Shift-JIS"), "shift_jis");
  assert.equal(normalizeEncodingLabel("Windows-31J"), "shift_jis");
  assert.equal(normalizeEncodingLabel("EUC_JP"), "euc-jp");
  assert.equal(normalizeEncodingLabel("EUC-KR"), "euc-kr");
  assert.equal(normalizeEncodingLabel("CP949"), "windows-949");
});

test("BOM and strict UTF-8 decisions are deterministic and traceable", () => {
  const utf8 = decodeSourceBytes(Buffer.from("한국어와日本語", "utf8"));
  assert.equal(utf8.text, "한국어와日本語");
  assert.equal(utf8.bomLength, 0);
  assert.equal(utf8.decision.canonicalLabel, "utf-8");
  assert.equal(utf8.decision.decisionSource, "strict_utf8");
  assert.equal(utf8.decision.confidence, 1);

  const bom = decodeSourceBytes(Buffer.concat([
    Buffer.from([0xef, 0xbb, 0xbf]),
    Buffer.from("日本語", "utf8"),
  ]));
  assert.equal(bom.text, "日本語");
  assert.equal(bom.bomLength, 3);
  assert.equal(bom.bomPolicy, "UTF8_BOM");
  assert.equal(bom.decision.decisionSource, "bom");
});

test("explicit legacy encodings decode strictly without replacement characters", () => {
  const fixtures = [
    { bytes: SHIFT_JIS_JAPANESE, encoding: "windows-31j", text: "日本語です。", canonical: "shift_jis" },
    { bytes: EUC_JP_JAPANESE, encoding: "euc-jp", text: "日本語です。", canonical: "euc-jp" },
    { bytes: EUC_KR_KOREAN, encoding: "euc-kr", text: "한국어", canonical: "euc-kr" },
    { bytes: EUC_KR_KOREAN, encoding: "cp949", text: "한국어", canonical: "windows-949" },
  ] as const;
  for (const fixture of fixtures) {
    const result = decodeSourceBytes(fixture.bytes, { explicitEncoding: fixture.encoding });
    assert.equal(result.text, fixture.text, fixture.encoding);
    assert.equal(result.text.includes("\uFFFD"), false, fixture.encoding);
    assert.equal(result.decision.canonicalLabel, fixture.canonical, fixture.encoding);
    assert.equal(result.decision.decisionSource, "user", fixture.encoding);
  }
});

test("a high-confidence legacy Japanese candidate may be selected without OS locale", () => {
  const result = decodeSourceBytes(Buffer.concat([
    SHIFT_JIS_JAPANESE,
    SHIFT_JIS_JAPANESE,
    SHIFT_JIS_JAPANESE,
  ]), { languageHint: "ja" });
  assert.equal(result.decision.canonicalLabel, "shift_jis");
  assert.equal(result.decision.decisionSource, "heuristic");
  assert.ok(result.decision.confidence >= 0.85);
  assert.equal(result.text, "日本語です。".repeat(3));
});

test("overlapping EUC-KR and Windows-949 candidates remain an explicit ambiguity", () => {
  assert.throws(
    () => decodeSourceBytes(Buffer.concat([
      EUC_KR_KOREAN,
      EUC_KR_KOREAN,
      EUC_KR_KOREAN,
    ]), { languageHint: "ko" }),
    (error: unknown) => {
      assert.ok(error instanceof EncodingPolicyError);
      assert.equal(error.code, "SOURCE_ENCODING_AMBIGUOUS");
      assert.deepEqual(
        error.alternatives.map((item) => item.canonicalLabel).sort(),
        ["euc-kr", "windows-949"],
      );
      assert.equal(JSON.stringify(error).includes("한국어"), false);
      return true;
    },
  );
});

test("unsupported labels and undecodable bytes fail with stable safe codes", () => {
  assert.throws(
    () => decodeSourceBytes(Buffer.from("plain"), { explicitEncoding: "utf-7" }),
    (error: unknown) => error instanceof EncodingPolicyError
      && error.code === "SOURCE_ENCODING_UNSUPPORTED",
  );
  assert.throws(
    () => decodeSourceBytes(Buffer.from([0x80, 0x81, 0x82]), { languageHint: "ko" }),
    (error: unknown) => error instanceof EncodingPolicyError
      && (error.code === "SOURCE_ENCODING_UNSUPPORTED"
        || error.code === "SOURCE_ENCODING_AMBIGUOUS"),
  );
});
