/**
 * Screenshot with the caret visible.
 *
 * Playwright hides the caret by default, and `caret: 'initial'` catches it
 * mid-blink half the time, so a focused frame whose only indicator IS the
 * caret (a glass pane does not change on focus; the caret is what shows the
 * input has it) came out identical to the resting one.
 *
 * `probe` is what the on/off comparison reads: the focused input alone, never
 * a region with motion of its own (a pulsing approval glow, a list's running
 * dots), which would differ between two takes with no caret at all. The caret
 * is on for ~500ms at a time; a probe before AND after the target shot both
 * showing it means the target shot fell inside one on-phase. Writes the
 * caret-on take to `opts.path` and returns true; after `attempts` misses
 * writes a caret-less take and returns false so the caller can fail the run.
 */
import { writeFileSync } from 'node:fs'

export async function screenshotWithCaret(target, opts, probe, { attempts = 16, pauseMs = 70 } = {}) {
  if (!probe) throw new Error('screenshotWithCaret: a probe locator (the focused input) is required')
  const bare = await probe.screenshot({ caret: 'hide' })
  const on = async () => !(await probe.screenshot({ caret: 'initial' })).equals(bare)
  for (let i = 0; i < attempts; i++) {
    if (await on()) {
      const shot = await target.screenshot({ ...opts, path: undefined, caret: 'initial' })
      if (await on()) {
        writeFileSync(opts.path, shot)
        return true
      }
    }
    await new Promise(r => setTimeout(r, pauseMs))
  }
  await target.screenshot({ ...opts, caret: 'hide' })
  return false
}
