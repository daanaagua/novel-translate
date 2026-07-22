import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/desktop/**/*.test.ts", "src/desktop/**/*.test.tsx"],
  },
});
