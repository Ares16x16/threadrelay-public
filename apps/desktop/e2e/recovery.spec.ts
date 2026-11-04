// Browser UI only: no Tauri, native account, persistence or no-flush claim.
import { test, expect } from '@playwright/test';

test('reload and failed connection never imply a running session', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Open local workspace', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Worker state is unavailable');
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled();
  await page.reload();
  await expect(page.getByText('Stopped', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled();
  await expect(page.getByText('Mode and feed are unconfirmed.', { exact: false })).toBeVisible();
});

test('repeated current-view navigation does not fabricate a recovery transition', async ({ page }) => {
  await page.goto('/');
  const button = page.getByRole('button', { name: 'Dashboard', exact: true });
  await button.click();
  await button.click();
  await expect(button).toBeFocused();
  await expect(button).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled();
});
