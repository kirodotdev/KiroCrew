/**
 * Settings section icons sit on a coloured rounded tile, the way macOS / iOS
 * System Settings mark each section. The tile is opt-in per tab (`tile`, a
 * `--tile-*` token): the other hosts of this layout pass no tile and keep the
 * bare `--muted` icon, so a tile must never appear for a tab that did not ask.
 * Both branches render it — the mobile root list and the desktop rail — because
 * a section's mark is its identity and has to be the same on both.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'

let mobile = true
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const INDEX_CSS = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')

const TABS: SidePanelTab[] = [
  { key: 'chat', label: 'Chat', icon: <svg data-testid="chat-icon" />, tile: 'var(--tile-green)' },
  { key: 'about', label: 'About', icon: <svg data-testid="about-icon" /> },
]

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/settings${search}`]}>
      <SidePanelLayout title="Settings" tabs={TABS} rememberKey="test-icon-tile">
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

const tileOf = (iconTestId: string) => screen.getByTestId(iconTestId).parentElement as HTMLElement

describe('settings icon tiles', () => {
  beforeEach(() => { sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('paints the tile behind a tab that asks for one on the mobile root list, and only there', () => {
    mobile = true
    renderAt('')
    expect(tileOf('chat-icon').style.background).toBe('var(--tile-green)')
    expect(tileOf('about-icon').style.background).toBe('')
    expect(tileOf('about-icon').className).toContain('text-muted')
  })

  it('paints the same tile on the desktop rail', () => {
    mobile = false
    renderAt('?tab=chat')
    expect(tileOf('chat-icon').style.background).toBe('var(--tile-green)')
    expect(tileOf('about-icon').style.background).toBe('')
  })

  // The hues are fixed on purpose (a section's mark looks the same in every
  // theme), so they live outside the pack-overridable role set: one `:root`
  // block and one `[data-mode="light"]` block, both carrying every token the
  // Settings tabs reference.
  it('defines every --tile-* token for both polarities in index.css', () => {
    const settingsPage = readFileSync(resolve(process.cwd(), 'src/pages/SettingsPage.tsx'), 'utf-8')
    const used = new Set([...settingsPage.matchAll(/var\((--tile-[a-z]+)\)/g)].map(m => m[1]))
    expect(used.size).toBeGreaterThan(0)
    const root = INDEX_CSS.match(/:root \{\s*--tile-fg:[\s\S]*?\}/)?.[0] ?? ''
    const light = INDEX_CSS.match(/\[data-mode="light"\] \{\s*--tile-gray:[\s\S]*?\}/)?.[0] ?? ''
    for (const token of used) {
      expect(root, `${token} in :root`).toContain(`${token}:`)
      expect(light, `${token} in light`).toContain(`${token}:`)
    }
  })
})
