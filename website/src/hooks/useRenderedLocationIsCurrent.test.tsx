/**
 * useRenderedLocationIsCurrent — the check a self-correcting URL write runs
 * before it replaces the current history entry.
 *
 * The check a render captured must turn false as soon as history moves past
 * that render's location, before the router has rendered the newer entry, and
 * the next render's check must be true again.
 */
import { describe, expect, it } from 'vitest'
import { act, render } from '@testing-library/react'
import { Router, UNSAFE_createMemoryHistory as createMemoryHistory } from 'react-router-dom'
import { useRenderedLocationIsCurrent } from './useRenderedLocationIsCurrent'

describe('useRenderedLocationIsCurrent', () => {
  it('is false for a render whose location history has already left', () => {
    const history = createMemoryHistory({ initialEntries: ['/settings/display'] })
    const checks: Array<() => boolean> = []
    function Probe() {
      checks.push(useRenderedLocationIsCurrent())
      return null
    }
    const view = render(
      <Router location={history.location} navigator={history}>
        <Probe />
      </Router>,
    )
    const rendered = checks[checks.length - 1]
    expect(rendered()).toBe(true)

    // History moves; the router has not rendered the new entry yet.
    act(() => { history.replace('/settings/about') })
    expect(rendered()).toBe(false)

    view.rerender(
      <Router location={history.location} navigator={history}>
        <Probe />
      </Router>,
    )
    expect(checks[checks.length - 1]()).toBe(true)
  })

  it('reports current under a navigator with no readable location', () => {
    const history = createMemoryHistory({ initialEntries: ['/settings'] })
    const checks: Array<() => boolean> = []
    function Probe() {
      checks.push(useRenderedLocationIsCurrent())
      return null
    }
    const navigator = { createHref: history.createHref, go: history.go, push: history.push, replace: history.replace }
    render(
      <Router location={history.location} navigator={navigator}>
        <Probe />
      </Router>,
    )
    expect(checks[checks.length - 1]()).toBe(true)
  })
})
