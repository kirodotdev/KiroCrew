// The hand-off's tint rides the notice's SEVERITY axis rather than its own importance: a
// danger-coloured action on a `warn` notice reports the failure that notice is explicitly
// saying did not happen. Pinned here because the invariant is a class string — nothing else
// in the suite fails when the hue is wrong, and the hue is the whole signal.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import ErrorNotice from '../components/ErrorNotice'
import { initI18n } from '../i18n/all'

initI18n('en')

const handoff = () => screen.getByRole('button', { name: /agent/i })

describe("the error notice's agent hand-off", () => {
  // Both variants share one tint expression, so a regression can hide in either.
  for (const variant of ['block', 'inline'] as const) {
    it(`follows the warn severity on a ${variant} notice`, () => {
      render(<ErrorNotice message="the switch was withheld" variant={variant} askAgent warn />)
      expect(handoff().className).toContain('text-warn/80')
      expect(handoff().className).not.toContain('text-danger')
      expect(handoff().className).not.toContain('decoration-danger')
    })

    it(`keeps the danger default on a ${variant} notice reporting a real failure`, () => {
      render(<ErrorNotice message="the write failed" variant={variant} askAgent />)
      expect(handoff().className).toContain('text-danger/80')
      expect(handoff().className).not.toContain('text-warn')
      expect(handoff().className).not.toContain('decoration-warn')
    })
  }
})
