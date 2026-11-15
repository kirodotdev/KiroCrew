/**
 * `askAgent: false` withholds a `block` or `page` failure notice's agent hand-off.
 *
 * A lazy part beside a draft the hand-off would discard (a side panel tab next to
 * the chat composer and open file edits) still shows its failure in its own
 * place, with Try Again and Reload page, but offers no "Ask the agent". Try Again
 * still imports the chunk again.
 */
import '../i18n/all'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { lazyRetry } from '../components/lazyRetry'
import { i18nT } from '../i18n/t'

/** A named ceiling for the lazy import, its rejection and the boundary's commit on a loaded worker. */
const CHUNK_SETTLE_TIMEOUT_MS = 5000

const chunk = { fail: true, imports: 0 }

// The part's chunk: an import that rejects the way a browser rejects a module
// script it could not fetch, until the test restores it.
function load() {
  chunk.imports += 1
  if (chunk.fail) return Promise.reject(new TypeError('Importing a module script failed.'))
  return Promise.resolve({ default: () => <div data-testid="part-loaded" /> })
}

beforeEach(() => {
  chunk.fail = true
  chunk.imports = 0
  // React logs the caught render error; the boundary handling it is the point.
  vi.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => { vi.restoreAllMocks() })

describe.each(['block', 'page'] as const)('lazyRetry %s layout with askAgent: false', layout => {
  it('shows the failure with Try Again and Reload page, no hand-off, and Try Again imports again', async () => {
    const Part = lazyRetry(load, { layout, title: () => 'The part could not load.', scope: 'test-part', askAgent: false })
    render(<Part />)

    const tryAgain = await screen.findByRole(
      'button', { name: i18nT('components.errorBoundary.try_again') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS },
    )
    expect(screen.getByText('The part could not load.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: i18nT('components.errorBoundary.reload_page') })).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: i18nT('components.askAgent.ask_the_agent') }),
      'askAgent: false offers no hand-off',
    ).toBeNull()
    expect(chunk.imports).toBe(1)

    chunk.fail = false
    fireEvent.click(tryAgain)
    expect(await screen.findByTestId('part-loaded', {}, { timeout: CHUNK_SETTLE_TIMEOUT_MS })).toBeInTheDocument()
    expect(chunk.imports, 'Try Again runs the import again').toBe(2)
  })

  it('offers the hand-off when askAgent is left unset', async () => {
    const Part = lazyRetry(load, { layout, title: () => 'The part could not load.', scope: 'test-part' })
    render(<Part />)
    expect(
      await screen.findByRole('button', { name: i18nT('components.askAgent.ask_the_agent') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS }),
    ).toBeInTheDocument()
  })
})
