/**
 * The root render tree must make Framer Motion follow the system "reduce
 * motion" setting.
 *
 * The CSS reset in `index.css` shortens CSS animations only. Framer animates in
 * JS, so without a root `MotionConfig` a slide or a spring still plays for a user
 * who asked for no motion. `AppMotionConfig` sets `reducedMotion="user"`, which
 * makes Framer jump transform animations straight to their end value.
 */
import { readFileSync } from 'fs'
import path from 'path'
import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { motion, useReducedMotionConfig } from 'framer-motion'
import { AppMotionConfig } from '../AppMotionConfig'

/**
 * Report "reduce motion" for every query that asks about it. Framer reads the
 * preference once into module state, so this file only covers the reduced case.
 */
function stubReducedMotion(): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('prefers-reduced-motion'),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
}

function ReducedProbe() {
  return <span data-testid="probe">{String(useReducedMotionConfig())}</span>
}

describe('AppMotionConfig', () => {
  beforeAll(stubReducedMotion)
  afterAll(() => vi.unstubAllGlobals())

  it('turns Framer reduced motion on when the system asks for it', () => {
    render(
      <AppMotionConfig>
        <ReducedProbe />
      </AppMotionConfig>
    )
    expect(screen.getByTestId('probe').textContent).toBe('true')
  })

  it('leaves Framer reduced motion off without it, even when the system asks', () => {
    render(<ReducedProbe />)
    expect(screen.getByTestId('probe').textContent).toBe('false')
  })

  it('jumps a slow slide straight to its end value', async () => {
    const onComplete = vi.fn()
    render(
      <AppMotionConfig>
        <motion.div
          data-testid="slide"
          initial={{ x: 0 }}
          animate={{ x: 100 }}
          transition={{ duration: 60 }}
          onAnimationComplete={onComplete}
        />
      </AppMotionConfig>
    )
    // A 60 s slide can only finish within the waitFor window if it was skipped.
    await waitFor(() => expect(onComplete).toHaveBeenCalled())
    expect(screen.getByTestId('slide').style.transform).toBe('translateX(100px)')
  })

  it('wraps the whole render tree in main.tsx', () => {
    const src = readFileSync(path.resolve(__dirname, '../main.tsx'), 'utf8')
    expect(src).toContain('.render(<AppMotionConfig>{rootTree}</AppMotionConfig>)')
  })
})
