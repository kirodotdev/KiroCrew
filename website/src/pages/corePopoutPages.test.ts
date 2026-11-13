/**
 * Task Runner keeps its pop-out window after it stopped being the built-in app
 * `projects`: the rail's pop-out action and the pop-out frame both judge a route
 * with `hasPopoutPage`.
 */
import { createElement } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CorePopoutPage, corePopoutPage, hasPopoutPage } from './corePopoutPages'
import { hasBuiltinComponent } from '../apps/builtinRegistry'
import { appRouteOf } from '../utils/appPopoutRoute'

vi.mock('./ProjectsPage', () => ({
  default: () => createElement('div', { 'data-testid': 'projects-page' }),
}))

describe('core pop-out pages', () => {
  it('Task Runner is a core page, no longer a built-in app route, and still pops out', () => {
    expect(hasBuiltinComponent('/projects')).toBe(false)
    expect(hasPopoutPage('/projects')).toBe(true)
    expect(corePopoutPage('/projects')?.labelKey).toBe('nav.task_runner')
  })

  it('a sub-path stays inside its window, and an unknown route is not a pop-out', () => {
    expect(appRouteOf('/projects/run-1', hasPopoutPage)).toBe('/projects')
    expect(appRouteOf('/schedule', hasPopoutPage)).toBeNull()
    expect(hasPopoutPage('/constructor')).toBe(false)
    expect(corePopoutPage('/toString')).toBeUndefined()
  })

  it('the window renders Task Runner\'s own page', async () => {
    render(createElement(CorePopoutPage, { entry: corePopoutPage('/projects')! }))
    // Waits through the entry's React.lazy ProjectsPage import inside its Suspense.
    expect(await screen.findByTestId('projects-page', {}, { timeout: 5000 })).toBeInTheDocument()
  })

  it('a built-in app route still pops out', () => {
    expect(hasPopoutPage('/md-notebook')).toBe(true)
  })
})
