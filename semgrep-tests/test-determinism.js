// Fixtures for semgrep/test-determinism.yaml (JavaScript tests), exercised by
// `semgrep --test` in the SAST job. `ruleid:` must match the next line; `ok:` must not.

async function promiseSleeps() {
  // ruleid: kirocrew.test-promise-sleep
  await new Promise((r) => setTimeout(r, 50));
  // ruleid: kirocrew.test-promise-sleep
  await new Promise((resolve) => {
    setTimeout(resolve, 50);
  });
  // ok: kirocrew.test-promise-sleep
  await new Promise((r) => setTimeout(r, 0));
}
