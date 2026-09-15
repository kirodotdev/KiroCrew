/**
 * `NativeSelect` — the touch path of `SimpleSelect` — and the two things a caller
 * must not be able to break.
 *
 * The chevron is drawn by this component as an absolutely-positioned overlay at
 * `right-3` inside the wrapper, so the control below it needs a right gutter or
 * the arrow paints straight over the selected text. `cn` is tailwind-merge and
 * last-wins, so a caller passing a shorthand like `px-1.5` used to set `pr` too
 * and take that gutter away — which is exactly what shipped on the phone
 * sidebar's recency-unit picker ("hou⌄rs").
 *
 * The wrapper, not the `<select>`, is the layout box: the control is `w-full`
 * inside it, so a caller's flex basis or min-width has no effect unless it lands
 * on the wrapper. That also makes the two `SimpleSelect` paths agree, since the
 * Radix path already puts `style` on its wrapper.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { NativeSelect, NativeSelectOption } from '../components/ui/native-select'

/** The gutter is restated here on purpose. Importing the component's own
 *  constant would make every assertion below true by construction — it would
 *  only prove the padding equals whatever it was set from, and a wrong value
 *  would sail through. Stated independently, a changed gutter reddens this. */
const CHEVRON_GUTTER = '2.25rem'

// The touch branch is what this file is about: on a mouse device SimpleSelect
// renders the Radix popup instead and there is no native control to inspect.
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => true }))

import SimpleSelect from '../components/SimpleSelect'

/** The rendered `<select>`, plus the positioning wrapper that owns the chevron. */
function renderSelect(props: Record<string, unknown> = {}) {
  const { container } = render(
    <NativeSelect aria-label="unit" value="hours" onChange={() => {}} {...props}>
      <NativeSelectOption value="hours">hours</NativeSelectOption>
    </NativeSelect>,
  )
  const select = container.querySelector('select') as HTMLSelectElement
  return { select, wrapper: select.parentElement as HTMLElement }
}

describe('NativeSelect', () => {
  it('reserves the chevron gutter even when a caller sets its own padding', () => {
    const { select } = renderSelect({ className: 'px-1.5 py-0.5 text-[12px] rounded' })
    // Inline, so it does not depend on tailwind-merge splitting a `px-*`
    // shorthand (it cannot) or on Tailwind's stylesheet order.
    expect(select.style.paddingInlineEnd).toBe(CHEVRON_GUTTER)
    // The caller's other classes still apply.
    expect(select.className.split(/\s+/)).toContain('py-0.5')
  })

  it('reserves the same gutter with no caller className at all', () => {
    const { select } = renderSelect()
    expect(select.style.paddingInlineEnd).toBe(CHEVRON_GUTTER)
  })

  it('puts a caller style on the wrapper, where the layout box is', () => {
    const { select, wrapper } = renderSelect({ wrapperStyle: { flex: '1 1 0%', minWidth: 0 } })
    expect(wrapper.style.flexGrow).toBe('1')
    expect(wrapper.style.minWidth).toBe('0')
    // The control itself is w-full inside the wrapper; a flex rule there is inert.
    expect(select.style.flexGrow).toBe('')
  })

  it('keeps the chevron inside the wrapper so the gutter and the arrow agree', () => {
    const { wrapper } = renderSelect()
    expect(wrapper.className.split(/\s+/)).toContain('relative')
    const chevron = wrapper.querySelector('svg')
    expect(chevron).not.toBeNull()
    expect(chevron?.getAttribute('class') ?? '').toContain('right-3')
  })
})

/**
 * The routing itself, through the component the sidebar actually calls. Asserting
 * `NativeSelect` alone would stay green if `SimpleSelect` handed `style` to the
 * control instead of the wrapper — which is the bug that shipped.
 */
describe('SimpleSelect on a touch device', () => {
  it('routes a caller style to the wrapper, not to the native control', () => {
    const { container } = render(
      <SimpleSelect
        value="hours"
        onChange={() => {}}
        options={['minutes', 'hours', 'days']}
        aria-label="unit"
        className="px-1.5 py-0.5 text-[12px] rounded"
        style={{ flex: '1 1 0%', minWidth: 0 }}
      />,
    )
    const select = container.querySelector('select') as HTMLSelectElement
    expect(select).not.toBeNull()
    const wrapper = select.parentElement as HTMLElement

    expect(wrapper.style.flexGrow).toBe('1')
    expect(select.style.flexGrow).toBe('')
    // …and the gutter survives the caller's `px-1.5` on the way through.
    expect(select.style.paddingInlineEnd).toBe(CHEVRON_GUTTER)
  })
})
