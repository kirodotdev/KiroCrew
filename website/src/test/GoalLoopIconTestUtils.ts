import { expect } from 'vitest'

/**
 * The goal-loop row shows the loop glyph only; the cycle count lives on the
 * glyph's tooltip and accessible name, never as visible "Loop N/M" text.
 */
export function expectLoopIcon(container: HTMLElement, count: number, total: number): void {
  const name = `Goal loop · cycle ${count} of ${total}`
  const icon = container.querySelector('[data-testid="goal-loop-icon"]')
  expect(icon).toBeTruthy()
  expect(icon!.getAttribute('role')).toBe('img')
  expect(icon!.getAttribute('aria-label')).toBe(name)
  expect(icon!.getAttribute('title')).toBe(name)
  expect(icon!.querySelector('.lucide-goal')).toBeTruthy()
  expect(container.textContent ?? '').not.toContain(`Loop ${count}/${total}`)
}
