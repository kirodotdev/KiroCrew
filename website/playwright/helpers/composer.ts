import { expect, type Locator, type Page } from '@playwright/test'

/**
 * The main chat composer's editable element.
 *
 * Every chat surface mounts the Lexical composer by default, whose editable
 * root is a contenteditable `<div data-composer-input>`: it has no `placeholder`
 * attribute (the placeholder is a sibling overlay) and no `value`, so
 * `getByPlaceholder(/message/i)` matches nothing and `toHaveValue` cannot read
 * it. The `data-composer-input` hook is the one sanctioned lookup (it is what
 * `pages/chat/composerFocus.ts` uses) and it also matches the `<textarea>`
 * that remains the chunk-load-failure fallback. The side chat mounts the same
 * component, so its composer is excluded the way `queryComposer()` excludes it.
 */
export function composer(page: Page): Locator {
  return page.locator('[data-composer-input]:not([data-side-chat-input] *)')
}

/** The composer's text as the app sees it — `value` on a textarea, text content on the Lexical root. */
export function composerText(locator: Locator): Promise<string> {
  return locator.evaluate(el => (el instanceof HTMLTextAreaElement ? el.value : (el.textContent ?? '')))
}

/** Assert the composer's text (polls, so it tolerates the send-then-clear frame). */
export async function expectComposerText(locator: Locator, expected: string, options: { timeout?: number } = {}): Promise<void> {
  await expect.poll(() => composerText(locator), options).toBe(expected)
}
