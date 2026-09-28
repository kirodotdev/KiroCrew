/**
 * Frame ONE settings row for a screenshot, from the union of its label's box and
 * its control's box, padded.
 *
 * Shared because two harnesses need the same geometry and `jscpd` runs at a 0%
 * threshold: the second copy of this block is a gate failure, not a style note.
 *
 * A `div:has-text(...)` wrapper is not usable for this. The deepest match is an
 * inner leaf and produced 2 KB near-empty crops, while the outermost is the whole
 * scroll panel -- so the crop is computed from the two boxes that actually matter.
 */
import { join } from 'node:path'

/**
 * @param page     Playwright page, already showing the row.
 * @param label    Locator for the row's label text.
 * @param control  Locator for the row's control (switch, combobox, input).
 * @param outDir   Directory to write into.
 * @param name     File name, e.g. '01-row-default.png'.
 * @param pad      Padding in CSS px around the union box (default 18).
 */
export async function shotSettingRow(page, { label, control, outDir, name, pad = 18 }) {
  const a = await label.boundingBox()
  const b = await control.boundingBox()
  if (!a || !b) throw new Error(`row not visible: label=${!!a} control=${!!b}`)
  const x = Math.max(0, Math.min(a.x, b.x) - pad)
  const y = Math.max(0, Math.min(a.y, b.y) - pad)
  const clip = {
    x,
    y,
    width: Math.max(a.x + a.width, b.x + b.width) - x + pad,
    height: Math.max(a.y + a.height, b.y + b.height) - y + pad,
  }
  const out = join(outDir, name)
  await page.screenshot({ path: out, clip })
  console.log('wrote', out, `${Math.round(clip.width)}x${Math.round(clip.height)}`)
  return out
}
