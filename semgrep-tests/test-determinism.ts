// Fixtures for semgrep/test-determinism.yaml (TypeScript rules), exercised by
// `semgrep --test` in the SAST job. `ruleid:` must match the next line; `ok:` must not.

async function promiseSleeps() {
  // ruleid: kirocrew.test-promise-sleep
  await new Promise((r) => setTimeout(r, 50));
  // ok: kirocrew.test-promise-sleep
  await new Promise((r) => setTimeout(r, 0));
}

async function playwright(page: any, expect: any) {
  // ruleid: kirocrew.test-wait-for-timeout
  await page.waitForTimeout(400);
  // ok: kirocrew.test-wait-for-timeout
  await expect(page.getByText("ready")).toBeVisible();
}
