// Requires @playwright/test and an available browser runtime. This does not
// exercise native Tauri or real accounts; it covers browser-rendered UI only.
import { test, expect } from '@playwright/test';

test('opening and navigating stays stopped and shows honest capability states', async ({ page }) => {
  await page.goto('http://127.0.0.1:1420');
  await expect(page.getByText('Stopped', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled();
  // Clicking the current view is intentionally a no-op; do not expect a focus transition.
  await page.getByRole('button', { name: 'Dashboard', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Dashboard', exact: true })).toBeFocused();
  for (const view of ['Review inbox', 'Topics', 'Routes', 'History', 'Accounts', 'Settings', 'Dashboard']) {
    await page.getByRole('button', { name: view, exact: true }).click();
    await expect(page.getByRole('heading', { level: 1, name: view })).toBeFocused();
  }
  await page.getByRole('button', { name: 'Accounts', exact: true }).click();
  await expect(page.getByText(/normal browser does not transfer/i)).toBeVisible();
  await expect(page.getByText(/No connection is attempted by this view/i)).toBeVisible();
});

test('navigation remains usable in narrow and dark reduced-motion presentation', async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 720 });
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
  await page.goto('http://127.0.0.1:1420');
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});
