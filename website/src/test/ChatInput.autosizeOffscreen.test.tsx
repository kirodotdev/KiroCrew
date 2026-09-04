import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import ChatInput from '../components/ChatInput'
import { renderWithProviders, createTestStore } from './helpers'

/**
 * REGRESSION GUARD — auto-sizing the composer must not touch its own box.
 *
 * The textarea is a flex ITEM, so its height is what the transcript scroller
 * above it does not get. Measured on the real dashboard at 390px: growing the
 * composer 44 -> 140px moved the scroller's clientHeight 561 -> 465px, one for
 * one. A taller scroller has a SMALLER maximum scrollTop, so the engine clamps
 * any reader parked closer to the bottom than the textarea is tall — and the
 * reader lands at the end with no application write anywhere to instrument.
 *
 * The autosizer used to set `height:'0'` on the live element and read
 * `scrollHeight` back, a forced synchronous layout in which the scroller was
 * momentarily taller by the textarea's whole height. Its comment claimed
 * `overflow:hidden` hid that from the parent; overflow governs scrollbars, not a
 * flex item's contribution, so the claim was false and the symptom it named
 * ("visible vibration") was the reported defect.
 *
 * Why no Chromium test can catch this behaviourally: Blink defers scroll-offset
 * clamping to the rendering lifecycle, so a transient undone inside the same task
 * never clamps, while WebKit clamps during layout. jsdom has no layout at all. So
 * these pins are STRUCTURAL — they assert the measurement cannot reach the live
 * element — plus one behavioural check that typing still resizes the composer.
 */

const SRC = readFileSync(join(__dirname, '..', 'components', 'ChatInput.tsx'), 'utf8')

/** The autosizer, sliced from its signature to its closing brace. */
function applyHeightSource(): string {
  const start = SRC.indexOf('function applyHeight(')
  expect(start).toBeGreaterThan(-1)
  const end = SRC.indexOf('\n}', start)
  expect(end).toBeGreaterThan(start)
  return SRC.slice(start, end)
}

describe('composer autosize measures off-screen', () => {
  it('never assigns a measurement height to the live element', () => {
    const body = applyHeightSource()
    // The only height write left is the FINAL one, guarded by a change check.
    expect(body).not.toMatch(/style\.height\s*=\s*'0'/)
    expect(body).not.toMatch(/style\.height\s*=\s*"0"/)
    expect(body).toMatch(/if \(next !== prev\) \{/)
    expect(body).toMatch(/el\.style\.height = next/)
    // The height write is the only one, and it publishes its cause so the
    // transcript can decline to chase it.
    expect(body).toMatch(/markComposerResize\(\)/)
    // Measurement is delegated, so the live element's own scrollHeight is not the
    // source of the answer any more.
    expect(body).toMatch(/measuredContentHeight\(el\)/)
    expect(body).not.toMatch(/Math\.min\(el\.scrollHeight/)
  })

  it('measures on a twin that is out of every flow', () => {
    // `position:fixed` is the load-bearing part: a twin in flow would move the
    // composer's own parent and reintroduce the defect through the back door.
    const twin = SRC.slice(SRC.indexOf('function measuredContentHeight('))
    expect(twin).toMatch(/style\.position = 'fixed'/)
    expect(twin).toMatch(/style\.visibility = 'hidden'/)
    expect(twin).toMatch(/style\.top = TWIN_OFFSCREEN_PX/)
    // Wrap-affecting properties must be copied or the twin wraps at a different
    // column and reports a height the live element would never have.
    for (const prop of ['width', 'boxSizing', 'paddingLeft', 'paddingRight', 'fontSize', 'lineHeight', 'whiteSpace', 'tabSize', 'wordSpacing', 'direction']) {
      expect(twin).toContain(`'${prop}'`)
    }
    expect(twin).toMatch(/for \(const prop of COPIED\)/)
    // An empty composer is measured with its placeholder text, which renders in the
    // content box and can wrap to two lines at phone width.
    expect(twin).toMatch(/el\.value \|\| el\.placeholder/)
  })

  it('routes the measurement through an off-screen twin carrying the live value', () => {
    // Behavioural floor that jsdom can honestly check. It has no layout, so the
    // HEIGHT cannot be asserted here without mocking the very thing under test —
    // but the twin's existence and its value are real, observable facts, and they
    // fail if the autosizer stops measuring or starts measuring the live element.
    // Controlled component: the value arrives as a PROP, so drive it that way —
    // firing a change event only calls onChange and leaves the element empty.
    const draft = 'a\nb\nc\nd\ne\nf'
    const { container } = renderWithProviders(
      <ChatInput value={draft} onChange={() => {}} onSend={() => {}} />,
      { store: createTestStore() },
    )
    const ta = container.querySelector('textarea')
    expect(ta).not.toBeNull()

    const twins = Array.from(document.body.querySelectorAll('textarea[aria-hidden="true"]'))
      .filter(t => !container.contains(t))
    expect(twins.length).toBe(1)
    const twin = twins[0] as HTMLTextAreaElement
    expect(twin.value).toBe(draft)
    expect(twin.style.position).toBe('fixed')
    // The live element never carries the measurement sentinel.
    expect(ta!.style.height).not.toBe('0px')
  })
})

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })
