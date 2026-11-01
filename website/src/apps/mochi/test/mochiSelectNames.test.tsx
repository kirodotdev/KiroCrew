/**
 * Each Mochi drop-down is announced by its caption.
 *
 * The captions over these three `<select>`s were sibling `<div>`/`<span>`s with no
 * link to the box, so a screen reader read each one as an unnamed "combo box".
 * Querying by role AND name is what tells "named" from "merely next to some text":
 * `getByRole('combobox', { name })` finds nothing until the caption is tied to it.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { i18nT } from '../../../i18n/t'
import { stateLabel } from '../i18nKeys'
import { REQUIRED_STATES, OPTIONAL_STATES } from '../src/shared/appearanceTypes'
import type { WatchItem } from '../src/shared/watchlistTypes'

const WATCH_ITEM: WatchItem = {
  id: 'w-1',
  label: 'Price drop',
  kind: 'custom',
  target: 'the price of a thing',
  status: 'watching',
  priority: 'normal',
  createdAt: '2026-01-01T00:00:00.000Z',
  nextCheckAfter: '2026-01-01T00:10:00.000Z',
  checkCount: 0,
  failCount: 0,
  maxFailCount: 5,
  maxWatchDurationHours: 168,
  checkIntervalMins: 10,
  notifyOnChange: true,
  autoComplete: true,
  source: 'api',
  history: [],
}

// One shared mock; each panel reads only the calls it makes.
vi.mock('../src/mochiApi', () => ({
  api: {
    hasShell: true,
    getConfig: async () => ({
      mochi: { petName: 'Mochi', petInstance: 'self', language: '' },
      shortcuts: {
        toggleWindow: 'CommandOrControl+Shift+M',
        screenCapture: 'CommandOrControl+Shift+S',
        hideAll: 'CommandOrControl+Shift+H',
      },
      window: { chatAlwaysOnTop: true },
    }),
    getMochiTrustLevel: async () => 'default',
    getStats: async () => ({}),
    instancesList: async () => ({ state: 'ready', instances: [] }),
    onSettingsCloseRequested: () => () => {},
    getWatchlist: async () => [WATCH_ITEM],
    onWatchlistChanged: () => () => {},
    // A re-edit with no source sheet: rows come straight from the saved strips,
    // so the slot cards render without slicing an image on a canvas.
    galleryGetPackDetail: async () => ({
      sprite: { frameWidth: 32, frameHeight: 32, fps: 8 },
      animations: { idle: { content: 'data:image/png;base64,AAAA', format: 'png' } },
    }),
  },
}))

// The preview draws on a canvas, which jsdom does not provide.
vi.mock('../src/renderer/SpriteRenderer', () => ({ SpriteRenderer: () => null }))

const { SettingsPanel } = await import('../src/renderer/SettingsPanel')
const { SpriteImporter } = await import('../src/renderer/SpriteImporter')
const { WatchlistPanel } = await import('../src/renderer/WatchlistPanel')

describe('Mochi drop-downs are named by their captions', () => {
  it('Settings: a SelectField is named by its label', async () => {
    render(<SettingsPanel onClose={() => {}} />)
    expect(
      await screen.findByRole('combobox', { name: i18nT('apps.mochi.settingsPanel.language') }),
    ).toBeTruthy()
  })

  it('Sprite importer: each row picker is named by its slot caption', async () => {
    render(
      <SpriteImporter
        existingPack={{ id: 'p1', name: 'Pack', author: '', description: '' } as never}
        onDone={() => {}}
        onCancel={() => {}}
      />,
    )
    const required = REQUIRED_STATES[0]
    expect(
      await screen.findByRole('combobox', { name: `${stateLabel(required)} *` }),
    ).toBeTruthy()
    const optional = OPTIONAL_STATES[0]
    expect(screen.getByRole('combobox', { name: stateLabel(optional) })).toBeTruthy()
  })

  it('Watchlist: the priority picker is named "Priority"', async () => {
    const user = userEvent.setup()
    render(<WatchlistPanel visible onClose={() => {}} petName="Mochi" />)
    await user.click(await screen.findByText(WATCH_ITEM.label))
    expect(
      await screen.findByRole('combobox', { name: i18nT('apps.mochi.watchPanel.field.priority') }),
    ).toBeTruthy()
  })
})
