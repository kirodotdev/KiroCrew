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
 * Assert the footer "Enter to submit" hint is dimmed exactly when Save is
 * unavailable. The colour TOKEN is `text-muted-strong` in both states and only
 * opacity moves (see the `footer` comment in FolderConfigModal.tsx): the
 * `--muted` / `--muted-strong` pair has no fixed prominence order, since
 * `--muted-strong` is darker than `--muted` in most themes and therefore reads
 * stronger on light backgrounds and weaker on dark ones, so a token swap dimmed
 * on one polarity and BRIGHTENED on the other. Reducing opacity lowers contrast
 * in every theme.
 *
 * This still does the staleness job the token check did, and does it more
 * tightly: a frame captured before that fix carries the retired plain
 * `text-muted` token on its disabled frames, which is now rejected outright in
 * BOTH states. The label and notice assertions never touch the footer, so
 * without this a stale PNG re-saved under a new name would pass all of them.
 *
 * Matched by visible text, not a testid -- the span carries none -- but the
 * string is a fixed i18n key with no interpolation, so it is stable to find
 * within the open modal. Opacity is read COMPUTED rather than off the class
 * list, because the dim is applied as an inline style.
 */
export async function assertFooterToken(page, frameLabel, expectDisabled) {
  const hint = page.locator(MODAL).getByText('Enter to submit')
  const seen = await hint.evaluate(el => ({
    cls: el.className,
    opacity: Number.parseFloat(getComputedStyle(el).opacity),
  }))
  const cls = seen.cls || ''
  const hasMutedStrong = /\btext-muted-strong\b/.test(cls)
  // `text-muted` is a SUBSTRING of `text-muted-strong`, so a naive `includes`
  // check would pass on either token. Match the retired token as the exact
  // class boundary -- not followed by the "-strong" suffix -- with a negative
  // lookahead, since `\b` alone does not separate `text-muted` from
  // `text-muted-strong` (there is no non-word boundary between `d` and `-`).
  const hasRetiredPlainMuted = /\btext-muted\b(?!-strong)/.test(cls)
  if (!hasMutedStrong) {
    throw new Error(`frame ${frameLabel}: expected the footer token text-muted-strong in both states, got class="${cls}"`)
  }
  if (hasRetiredPlainMuted) {
    throw new Error(
      `frame ${frameLabel}: footer carries the RETIRED plain text-muted token, so this frame predates the `
      + `opacity-based dim and would document a state that inverted on dark themes, class="${cls}"`,
    )
  }
  if (expectDisabled && !(seen.opacity < 1)) {
    throw new Error(`frame ${frameLabel}: Save is disabled but the footer hint is not dimmed, opacity=${seen.opacity}`)
  }
  if (!expectDisabled && seen.opacity !== 1) {
    throw new Error(
      `frame ${frameLabel}: Save is enabled so the hint must render at full opacity, matching what main ships, `
      + `got opacity=${seen.opacity}`,
    )
  }
}
