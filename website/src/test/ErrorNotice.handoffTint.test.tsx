// The hand-off's tint is pinned because the invariant is a class string — nothing else in the
// suite fails when the hue is wrong, and the hue is the whole signal. There is deliberately no
// warn arm: every value this notice renders is the outcome of something that failed, a refusal
// that merely WITHHELD the action included, so the hand-off is danger-toned on every path.
// `errors-use-error-notice` (blocking) counts a warn- or status-toned failure as the same
// violation as a hand-rolled red div.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import ErrorNotice from '../components/ErrorNotice'
import { initI18n } from '../i18n/all'

initI18n('en')

const handoff = () => screen.getByRole('button', { name: /agent/i })

describe("the error notice's agent hand-off", () => {
  // Both variants share one tint expression, so a regression can hide in either.
  for (const variant of ['block', 'inline'] as const) {
    it(`is danger-toned on a ${variant} notice reporting a real failure`, () => {
      render(<ErrorNotice message="the write failed" variant={variant} askAgent />)
      expect(handoff().className).toContain('text-danger/80')
      expect(handoff().className).not.toContain('text-warn')
      expect(handoff().className).not.toContain('decoration-warn')
    })

    it(`is danger-toned on a ${variant} notice whose action was WITHHELD`, () => {
      // The case that used to be warn. A withheld action is still a rejected request, so the
      // hand-off must not soften — this is the arm a re-introduced severity axis would redden.
      render(<ErrorNotice message="the switch was withheld" variant={variant} askAgent />)
      expect(handoff().className).toContain('text-danger/80')
      expect(handoff().className).not.toContain('text-warn')
      expect(handoff().className).not.toContain('decoration-warn')
    })
  }
})
