import type { Page } from '@playwright/test'

/**
 * Pick a value from one of the shared dropdowns (`SimpleSelect` /
 * `SettingsSelect` / `SearchableSelect`).
 *
 * These are Radix, not a native `<select>`, so `selectOption()` does not apply —
 * the trigger is a `<button role="combobox">` and the rows only exist in the DOM
 * while the popup is open, portalled to the end of `<body>`. Open it, then click
 * the row.
 *
 * `name` is the dropdown's accessible name (its `aria-label`; a `<label for>`
 * does NOT name a button). `option` is the row's VISIBLE label, which is often
 * not the underlying value — the artifacts kind filter stores `widget` and
 * renders `kind: widget`.
 */
export async function pickFromDropdown(
  page: Page,
  name: string | RegExp,
  option: string | RegExp,
): Promise<void> {
  await page.getByRole('combobox', { name }).click()
  await page.getByRole('option', { name: option }).click()
  // The popup unmounts on commit; waiting for that keeps a following assertion
  // from racing the closing animation.
  await page.getByRole('option', { name: option }).waitFor({ state: 'detached' })
}
