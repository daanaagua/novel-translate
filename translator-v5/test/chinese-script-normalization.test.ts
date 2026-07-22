import assert from "node:assert/strict";
import test from "node:test";

import { simplifyChineseTranslation } from "../src/style/chinese-script-normalization.js";

test("traditional Chinese prose is normalized to simplified Chinese", () => {
  assert.equal(
    simplifyChineseTranslation("黑殺隊的訓練結束了，後來……"),
    "黑杀队的训练结束了，后来……",
  );
});

test("locked target forms survive script normalization verbatim", () => {
  assert.equal(
    simplifyChineseTranslation("龍與後來的龍同行。", ["龍"]),
    "龍与后来的龍同行。",
  );
});
