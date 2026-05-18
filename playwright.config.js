// @ts-check
const { defineConfig } = require('/opt/node22/lib/node_modules/playwright/test.js');

module.exports = defineConfig({
  testDir: './tests/e2e',
  testMatch: '**/*.spec.js',
  fullyParallel: false,
  retries: 1,
  timeout: 30000,
  use: {
    headless: true,
    viewport: { width: 1280, height: 800 },
    ignoreHTTPSErrors: true,
    browserName: 'chromium',
  },
});
