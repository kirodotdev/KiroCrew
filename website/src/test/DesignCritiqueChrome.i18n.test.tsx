/**
 * Design Critique chrome under a non-English language, and the History menu's
 * keyboard contract.
 *
 * The rail's `History (n)` button, the screen list heading, the preview hint,
 * the step labels, the composer's access/start/remove labels and a finding's
 * fallback title and `Fix`/`Suggestion` label were English literals beside
 * translated text. These mount the real components in zh-CN and read what a
 * user would read; each expected value is read from the catalog and checked
 * against English first, so a missing key cannot pass by falling back.
 *
 * The History button opened a menu with no `aria-expanded` / `aria-haspopup`,
 * and Escape did not close it. A keyboard user had no way to dismiss it but to
 * pick an entry.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import React from 'react'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'
import { HKEY } from '../apps/design-critique/constants'
import { designCritiqueApi } from '../apps/design-critique/api'
import Composer from '../apps/design-critique/Composer'
import FindingRow from '../apps/design-critique/FindingRow'
import type { HistoryEntry, StagedItem } from '../apps/design-critique/types'

vi.mock('../apps/design-critique/api', () => ({
  designCritiqueApi: {
    openSlot: vi.fn(), getSlot: vi.fn(), send: vi.fn(), deleteSlot: vi.fn(), uploadFiles: vi.fn(),
    discover: vi.fn(), render: vi.fn(), pollDiscover: vi.fn(), pollRender: vi.fn(),
    method: vi.fn().mockResolvedValue({ skill: '', checklist: '' }),
  },
  fileUrl: (p: string) => '/api/file-raw?path=' + encodeURIComponent(p),
}))

const DesignCritiquePage = (await import('../apps/design-critique/DesignCritiquePage')).default

const K = 'apps.designCritique.'

/** The zh-CN value of `key`, guarded so a missing translation fails here. */
function zh(key: string, opts?: Record<string, unknown>): string {
  const en = i18next.getFixedT('en')(K + key, opts) as string
  const value = i18next.getFixedT('zh-CN')(K + key, opts) as string
  expect(value).not.toBe(en)
  expect(value).not.toBe(K + key)
  return value
}

function entry(id: number, over: Partial<HistoryEntry> = {}): HistoryEntry {
  return {
    id,
    // A fixed instant: the menu shows a relative time, and nothing here reads it.
    ts: 1_790_000_000_000 - id * 1000,
    slotKey: 'slot-' + id,
    screens: [
      { step: 1, label: 'Cart', url: '/api/file-raw?path=%2Ftmp%2Fa.png' },
      { step: 2, label: 'Pay', url: '/api/file-raw?path=%2Ftmp%2Fb.png' },
    ],
    thumbUrl: '/api/file-raw?path=%2Ftmp%2Fa.png',
    read: 'Critique number ' + id,
    report: {
      overallRead: 'Critique number ' + id,
      flow: { label: 'Checkout' },
      findings: [{ severity: 'minor', title: 'Finding ' + id, box: null }],
    },
    ...over,
  } as HistoryEntry
}

const noop = () => {}

function staged(n: number): StagedItem[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `zzz-${i}`,
    file: new File(['zzz'], `zzz-${i}.png`, { type: 'image/png' }),
    url: `blob:zzz-${i}`,
  }))
}

const composerProps = {
  staged: [], refText: '', dragging: false, blocked: null, showAuth: false, busy: false, err: '', hint: '',
  inputRef: React.createRef<HTMLInputElement>(),
  onPick: noop, onDrop: noop, onDragOver: noop, onDragLeave: noop, pickFile: noop, dropStaged: noop,
  moveStaged: noop, clearStaged: noop, start: noop, setRefText: noop, setBlocked: noop, setShowAuth: noop,
  onTryAgain: noop,
} as unknown as React.ComponentProps<typeof Composer>

beforeEach(() => { localStorage.clear() })
afterEach(async () => {
  cleanup()
  await i18next.changeLanguage('en')
})
afterAll(async () => { await i18next.changeLanguage('en') })

/** Open the newest critique so the rail shows its History button. */
function openReport(): HTMLElement {
  localStorage.setItem(HKEY, JSON.stringify([entry(1), entry(2)]))
  render(<DesignCritiquePage />)
  fireEvent.click(screen.getByText('Critique number 1'))
  return screen.getByTitle(i18next.t(K + 'designCritiquePage.switch_critique') as string)
}

describe('History menu button', () => {
  it('reports its state and its popup', () => {
    const btn = openReport()
    expect(btn).toHaveAttribute('aria-haspopup', 'menu')
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    expect(document.getElementById(btn.getAttribute('aria-controls') || '')).not.toBeNull()
  })

  it('closes on Escape and puts focus back on the button', () => {
    const btn = openReport()
    fireEvent.click(btn)
    const menu = document.getElementById(btn.getAttribute('aria-controls') || '')
    expect(menu).not.toBeNull()
    // Focus is somewhere inside the menu when the user presses Escape.
    expect(menu!.contains(document.activeElement)).toBe(true)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(document.getElementById(btn.getAttribute('aria-controls') || '')).toBeNull()
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    expect(document.activeElement).toBe(btn)
  })

  it('is a menu: focus starts on the first item and the arrow keys move it', () => {
    const btn = openReport()
    fireEvent.click(btn)
    expect(screen.getByRole('menu')).toBeInTheDocument()
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(2)
    expect(document.activeElement).toBe(items[0])
    fireEvent.keyDown(items[0], { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[1])
    fireEvent.keyDown(items[1], { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[0])
    fireEvent.keyDown(items[0], { key: 'End' })
    expect(document.activeElement).toBe(items[1])
    fireEvent.keyDown(items[1], { key: 'Home' })
    expect(document.activeElement).toBe(items[0])
    fireEvent.keyDown(items[0], { key: 'ArrowUp' })
    expect(document.activeElement).toBe(items[1])
    // Enter picks the focused item, and focus goes back to the button.
    fireEvent.keyDown(items[1], { key: 'Enter' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(screen.getByText('Finding 2')).toBeInTheDocument()
    expect(document.activeElement).toBe(btn)
  })

  it('keeps Tab inside the open menu', () => {
    const btn = openReport()
    fireEvent.click(btn)
    const items = screen.getAllByRole('menuitem')
    // Native Tab steps between the entries; the shared hook wraps at the ends.
    for (const it of items) expect(it.tabIndex).toBe(0)
    items[1].focus()
    fireEvent.keyDown(items[1], { key: 'Tab' })
    expect(document.activeElement).toBe(items[0])
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('closes on a pointer-down outside it', () => {
    const btn = openReport()
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    fireEvent.pointerDown(document.body)
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('still switches to another critique from the menu', () => {
    const btn = openReport()
    fireEvent.click(btn)
    fireEvent.click(screen.getAllByText('Critique number 2')[0])
    expect(screen.getByText('Finding 2')).toBeInTheDocument()
  })
})

describe('Design Critique chrome in zh-CN', () => {
  it('translates the History count, the screen list heading and the preview hint', async () => {
    await i18next.changeLanguage('zh-CN')
    openReport()
    expect(screen.getByText(zh('designCritiquePage.history_count', { n: '2' }))).toBeInTheDocument()
    expect(screen.getByText(new RegExp(zh('designCritiquePage.click_to_enlarge')))).toBeInTheDocument()
    expect(screen.getAllByTitle(zh('designCritiquePage.step_n', { n: '1' }) + ' · Cart').length).toBeGreaterThan(0)
    expect(screen.queryByText(/History \(/)).toBeNull()
  })

  it('translates the screen list heading while a flow is being read', async () => {
    await i18next.changeLanguage('zh-CN')
    const api = designCritiqueApi as unknown as Record<string, ReturnType<typeof vi.fn>>
    api.uploadFiles.mockResolvedValue({ paths: ['/tmp/a.png', '/tmp/b.png'] })
    api.openSlot.mockResolvedValue({ key: 'slot-1' })
    api.send.mockResolvedValue(undefined)
    api.getSlot.mockResolvedValue({ running: true, messages: [] })
    const { container } = render(<DesignCritiquePage />)
    const files = [new File(['p'], 'a.png', { type: 'image/png' }), new File(['p'], 'b.png', { type: 'image/png' })]
    fireEvent.drop(container.querySelector('[role="presentation"]')!, { dataTransfer: { files, types: ['Files'] } })
    fireEvent.click(screen.getByTitle(zh('composer.start_the_critique')))
    expect(await screen.findByText(zh('designCritiquePage.screens_in_order'))).toBeInTheDocument()
    expect(screen.queryByText('Screens in order')).toBeNull()
  })

  it('translates the composer step chip, remove label and start hint', async () => {
    await i18next.changeLanguage('zh-CN')
    render(<Composer {...composerProps} staged={staged(2)} />)
    expect(screen.getByText(zh('composer.step_n', { n: '1' }))).toBeInTheDocument()
    expect(screen.getByLabelText(zh('composer.remove_name', { name: 'zzz-0.png' }))).toBeInTheDocument()
    expect(screen.getByTitle(zh('composer.start_the_critique'))).toBeInTheDocument()
    cleanup()
    render(<Composer {...composerProps} />)
    expect(screen.getByTitle(zh('composer.add_screenshots_or_paste_a_link_first'))).toBeInTheDocument()
  })

  it('translates the access toggle both ways', async () => {
    await i18next.changeLanguage('zh-CN')
    const blocked = { say: 'zzz', hint: 'zzz', fix: 'shots', auth: { lead: 'l', cmds: ['c'], tail: 't' } }
    render(<Composer {...composerProps} blocked={blocked as never} />)
    expect(screen.getByRole('button', { name: zh('composer.fix_my_access') })).toBeInTheDocument()
    cleanup()
    render(<Composer {...composerProps} blocked={blocked as never} showAuth />)
    expect(screen.getByRole('button', { name: zh('composer.hide_access_steps') })).toBeInTheDocument()
  })

  it('translates a finding with no title, and its Fix and Suggestion labels', async () => {
    await i18next.changeLanguage('zh-CN')
    const row = (category: string) => (
      <FindingRow
        finding={{ severity: 'minor', title: '', category, fix: 'zzz fix' }} index={0} isOpen isActive={false}
        flowScoped={false} pinNum={undefined} stepRange="" withMarks={t => t}
        onEnter={noop} onLeave={noop} onClick={noop} rowRef={noop}
      />
    )
    render(row('Accessibility'))
    expect(screen.getByText(zh('findingRow.finding'))).toBeInTheDocument()
    expect(screen.getByText(zh('findingRow.fix'))).toBeInTheDocument()
    cleanup()
    render(row('Hierarchy'))
    expect(screen.getByText(zh('findingRow.suggestion'))).toBeInTheDocument()
  })
})
