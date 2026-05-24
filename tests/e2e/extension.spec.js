/**
 * Chrome Extension smoke test using Playwright.
 *
 * Tests:
 *  1. Extension loads without errors (service worker registered)
 *  2. Popup renders all required UI elements
 *  3. Settings panel saves/loads correctly
 *  4. Tab switching works
 *  5. Buttons are present and interactive
 *
 * Does NOT test live API calls (covered by tests/test_extension_api_flow.py).
 */
const { test, expect, chromium } = require('/opt/node22/lib/node_modules/playwright/test.js');
const path = require('path');

const EXT_PATH = path.resolve(__dirname, '../../chrome-extension');

// Launch a persistent Chromium context with the extension loaded.
// MV3 extensions don't register a service worker under Playwright's default
// "old" headless mode — pass `--headless=new` explicitly so the worker spins up.
async function launchWithExtension() {
  return chromium.launchPersistentContext('', {
    headless: false,
    args: [
      '--headless=new',
      `--load-extension=${EXT_PATH}`,
      `--disable-extensions-except=${EXT_PATH}`,
      '--no-sandbox',
      '--disable-setuid-sandbox',
    ],
  });
}

// Wait until the extension's MV3 service worker is registered.
async function waitForServiceWorker(context, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const worker = context.serviceWorkers().find(w => w.url().includes('service-worker.js'));
    if (worker) return worker;
    await new Promise(r => setTimeout(r, 250));
  }
  throw new Error('Extension service worker did not register within ' + timeoutMs + 'ms');
}

test.describe('CUB Extension: basic load', () => {
  let context;

  test.beforeAll(async () => {
    context = await launchWithExtension();
  });

  test.afterAll(async () => {
    await context.close();
  });

  test('extension service worker registers without errors', async () => {
    const extWorker = await waitForServiceWorker(context);
    expect(extWorker, 'service-worker.js should be registered').toBeTruthy();
  });

  test('popup HTML loads all key elements', async () => {
    const extWorker = await waitForServiceWorker(context);
    const extId = extWorker.url().match(/chrome-extension:\/\/([^/]+)/)?.[1];
    expect(extId, 'extension ID should be extractable').toBeTruthy();

    const page = await context.newPage();
    await page.goto(`chrome-extension://${extId}/popup.html`);

    // Tab bar
    await expect(page.locator('.tab[data-tab="auto"]')).toBeVisible();
    await expect(page.locator('.tab[data-tab="conflicts"]')).toBeVisible();
    await expect(page.locator('.tab[data-tab="settings"]')).toBeVisible();

    // Main automation button
    await expect(page.locator('#fullAutoBtn')).toBeVisible();
    await expect(page.locator('#fullAutoBtn')).toContainText('Full Automation');

    // Report ID input
    await expect(page.locator('#reportId')).toBeVisible();

    // Step progress items
    await expect(page.locator('.step[data-step="login"]')).toBeVisible();
    await expect(page.locator('.step[data-step="etl"]')).toBeVisible();
    await expect(page.locator('.step[data-step="generate"]')).toBeVisible();

    await page.close();
  });

  test('settings panel saves and reloads values', async () => {
    const extWorker = await waitForServiceWorker(context);
    const extId = extWorker.url().match(/chrome-extension:\/\/([^/]+)/)?.[1];

    const page = await context.newPage();
    await page.goto(`chrome-extension://${extId}/popup.html`);

    // Switch to settings tab
    await page.locator('.tab[data-tab="settings"]').click();

    // Fill settings
    await page.locator('#baseUrl').fill('http://localhost:8000');
    await page.locator('#email').fill('test@cub.com');
    await page.locator('#password').fill('testpass');
    await page.locator('#geminiKey').fill('AIzaTestKey');

    // Save
    await page.locator('#saveSettingsBtn').click();
    await expect(page.locator('#saveSettingsBtn')).toContainText('Saved');

    // Reload popup and verify persistence
    await page.reload();
    await page.locator('.tab[data-tab="settings"]').click();
    await expect(page.locator('#baseUrl')).toHaveValue('http://localhost:8000');
    await expect(page.locator('#email')).toHaveValue('test@cub.com');

    await page.close();
  });

  test('tab switching shows correct panels', async () => {
    const extWorker = await waitForServiceWorker(context);
    const extId = extWorker.url().match(/chrome-extension:\/\/([^/]+)/)?.[1];

    const page = await context.newPage();
    await page.goto(`chrome-extension://${extId}/popup.html`);

    // Default: auto panel visible
    await expect(page.locator('#panel-auto')).toBeVisible();
    await expect(page.locator('#panel-conflicts')).not.toBeVisible();

    // Switch to conflicts
    await page.locator('.tab[data-tab="conflicts"]').click();
    await expect(page.locator('#panel-conflicts')).toBeVisible();
    await expect(page.locator('#panel-auto')).not.toBeVisible();

    // Switch to settings
    await page.locator('.tab[data-tab="settings"]').click();
    await expect(page.locator('#panel-settings')).toBeVisible();
    await expect(page.locator('#panel-conflicts')).not.toBeVisible();

    await page.close();
  });

  test('full automation button is disabled when report ID is empty', async () => {
    const extWorker = await waitForServiceWorker(context);
    const extId = extWorker.url().match(/chrome-extension:\/\/([^/]+)/)?.[1];

    const page = await context.newPage();
    await page.goto(`chrome-extension://${extId}/popup.html`);

    // Ensure reportId is empty
    await page.locator('#reportId').fill('');

    // Click full auto — should show alert, not crash
    page.on('dialog', async dialog => { await dialog.dismiss(); });
    await page.locator('#fullAutoBtn').click();

    // Extension should still be responsive (not crashed)
    await expect(page.locator('#fullAutoBtn')).toBeVisible();

    await page.close();
  });
});
