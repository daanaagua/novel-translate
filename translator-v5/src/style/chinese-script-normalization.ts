import OpenCC from "opencc-js";

const toSimplified = OpenCC.Converter({ from: "t", to: "cn" });

function protectedForms(values: readonly string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))]
    .sort((left, right) => right.length - left.length || left.localeCompare(right));
}

/**
 * Converts model prose to zh-Hans while keeping explicit glossary targets
 * byte-for-byte intact. Protected strings are split out before conversion,
 * so OpenCC can never silently rewrite a user-locked spelling.
 */
export function simplifyChineseTranslation(
  text: string,
  preservedTargetForms: readonly string[] = [],
): string {
  const preserved = protectedForms(preservedTargetForms);
  if (text.length === 0 || preserved.length === 0) {
    return toSimplified(text);
  }

  let cursor = 0;
  let output = "";
  while (cursor < text.length) {
    let nextIndex = -1;
    let nextForm = "";
    for (const form of preserved) {
      const index = text.indexOf(form, cursor);
      if (index < 0) {
        continue;
      }
      if (nextIndex < 0 || index < nextIndex || (index === nextIndex && form.length > nextForm.length)) {
        nextIndex = index;
        nextForm = form;
      }
    }
    if (nextIndex < 0) {
      output += toSimplified(text.slice(cursor));
      break;
    }
    output += toSimplified(text.slice(cursor, nextIndex));
    output += nextForm;
    cursor = nextIndex + nextForm.length;
  }
  return output;
}
