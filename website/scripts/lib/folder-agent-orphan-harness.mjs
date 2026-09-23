/**
 * Shared selectors and footer-token assertion for the two folder-agent
 * orphan-state capture harnesses (`capture-folder-agent-dirless-pick.mjs`,
 * `capture-folder-agent-inherited-orphan.mjs`). Both scenes drive the same
 * `FolderConfigModal` surface -- the explicit-pick dir-less orphan and the
 * inherited-agent orphan respectively -- and had grown a byte-identical
 * region (the modal's data-testid selectors plus the footer "Enter to
 * submit" token check) that the frontend lint's duplicate-code gate
 * (`jscpd`, hard zero threshold) flags at 414 tokens. Shared here instead of
 * copied a second time.
 *
 * `assertFooterToken` takes no scene-specific parameter: the check itself
 * (`text-muted-strong` while Save is enabled, dimmed `text-muted` while
 * disabled) is identical in both harnesses, driven off each frame's own
 * `expectDisabled`, which the caller already varies per scene.
 */

export const MODAL = '[role="dialog"]'
export const ORPHAN_NOTICE = '[data-testid="folder-config-agent-notice"]'
export const SUBMIT = '[data-testid="folder-config-submit"]'
export const PROJECT_DIR = '[data-testid="folder-config-project-dir"]'
// The picker has no testid on purpose (the tests drive the shipped Radix
// combobox by ROLE); address it the same way so this harness cannot pass
// against a stub the users never see.

/**
 * Assert the footer "Enter to submit" hint carries the token that matches
 * `canSubmit` -- `text-muted-strong` while Save is enabled, dimmed to
 * `text-muted` while it is disabled (see the `footer` prop in
 * FolderConfigModal.tsx). This is what proves a frame was taken AFTER that
 * copy change landed rather than being a stale image re-saved under a new
 * name: the label/notice assertions never touch the footer, so a stale PNG
 * would pass all of them while still carrying the retired token.
 *
 * Matched by visible text, not a testid -- the span carries none -- but the
 * string is a fixed i18n key with no interpolation, so it is stable to find
 * within the open modal.
 */
export async function assertFooterToken(page, frameLabel, expectDisabled) {
  const hint = page.locator(MODAL).getByText('Enter to submit')
  const cls = (await hint.getAttribute('class')) || ''
  const wantMutedStrong = !expectDisabled
  const hasMutedStrong = /\btext-muted-strong\b/.test(cls)
  // `text-muted` is a SUBSTRING of `text-muted-strong`, so a naive `includes`
  // check would pass on either token. Match the disabled token as the exact
  // class boundary -- not preceded by the "-strong" suffix -- with a negative
  // lookahead, since `\b` alone does not separate `text-muted` from
  // `text-muted-strong` (there is no non-word boundary between `d` and `-`).
  const hasMutedPlain = /\btext-muted\b(?!-strong)/.test(cls)
  if (wantMutedStrong && !hasMutedStrong) {
    throw new Error(`frame ${frameLabel}: expected the enabled footer token text-muted-strong, got class="${cls}"`)
  }
  if (wantMutedStrong && hasMutedPlain) {
    throw new Error(`frame ${frameLabel}: footer carries the dimmed text-muted token while Save is enabled, class="${cls}"`)
  }
  if (!wantMutedStrong && !hasMutedPlain) {
    throw new Error(`frame ${frameLabel}: expected the dimmed footer token text-muted, got class="${cls}"`)
  }
  if (!wantMutedStrong && hasMutedStrong) {
    throw new Error(`frame ${frameLabel}: footer still carries the enabled text-muted-strong token while Save is disabled, class="${cls}"`)
  }
}
