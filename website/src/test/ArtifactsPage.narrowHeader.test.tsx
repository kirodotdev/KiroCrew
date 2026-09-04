/**
 * The artifact library's header row on a phone.
 *
 * Measured on a real build at 320px in zh-CN: the row ran 34px past the pane
 * with no scrollable ancestor (so those pixels were unreachable, not merely
 * off-screen), and the title was the flex item that gave — squeezed to 14px
 * while the view switcher still hung over the edge. Two things fix it and both
 * are asserted here, because either one alone still overflows:
 *
 *   - the header may wrap, so the action group takes its own line instead of
 *     crushing the title. The BUTTON row itself still does not wrap.
 *   - the view switcher goes icon-only. Its own responsive collapse cannot fire
 *     here: it measures its parent, and that parent hugs this control's width.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')
vi.mock('@virtuoso.dev/masonry', () => ({ VirtuosoMasonry: () => <div data-testid="masonry" /> }))

let mobile = true
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

/** The row that holds the title and the create/view controls. */
function headerRow(): HTMLElement {
  const title = screen.getByRole('heading', { name: /your artifacts/i })
  return title.parentElement as HTMLElement
}

describe('ArtifactsPage header row at phone width', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mobile = true
    localStorage.setItem('mc-artifacts-view', 'grid')
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).artifactSessionDocs = vi.fn().mockResolvedValue({ docs: [] })
  })
  afterEach(cleanup)

  it('lets the header reflow so the title is not the item that gives', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(headerRow()).toBeTruthy())
    expect(headerRow().className).toContain('flex-wrap')
  })

  it('drops the view switcher to icon-only, keeping the selected label', async () => {
    renderWithProviders(<ArtifactsPage />)
    // The selected view keeps its label so the current state stays readable;
    // the unselected one is the width that has to go.
    await waitFor(() => expect(screen.getByRole('button', { name: /gallery/i })).toBeTruthy())
    expect(screen.getByRole('button', { name: /gallery/i }).textContent).toContain('Gallery')
    expect(screen.getByRole('button', { name: /table/i }).textContent).toBe('')
  })

  it('moves the folder action off the row and into the add menu', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(headerRow()).toBeTruthy())
    // Not a peer button any more...
    expect(screen.queryByRole('button', { name: /new folder/i })).toBeNull()
    // ...but still reachable, one tap away, behind a trigger whose name covers
    // what the menu now holds: an add action AND an organize action. The
    // add-only name would under-promise it, and that name is all a screen
    // reader gets from a chevron.
    const trigger = screen.getByRole('button', { name: /more actions/i })
    // Radix opens on a pointer/keyboard event, not a synthetic click; Enter
    // also proves the menu is reachable without a pointer.
    fireEvent.keyDown(trigger, { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('menuitem', { name: /new folder/i })).toBeTruthy())
  })

  it('keeps the add-specific trigger name on desktop, where the menu only adds', async () => {
    mobile = false
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(headerRow()).toBeTruthy())
    expect(screen.getByRole('button', { name: /more ways to add an artifact/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /more actions/i })).toBeNull()
  })

  it('keeps the folder action on the row at desktop width', async () => {
    mobile = false
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /new folder/i })).toBeTruthy())
  })
})
