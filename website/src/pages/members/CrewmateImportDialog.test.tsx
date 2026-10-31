import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { Card } from '../../api/cards'

/* Import from file (rfc-crewmate-share.md §5.4): the dialog asks the gateway for
 * the import slot's pending cards, offers the `.crewmate` file picker, shows the
 * ONE card the gateway raises in the shared card component, and says why a file
 * was refused. It creates nothing itself. */

const pending = vi.fn<(slot: string) => Promise<Card[]>>()
const importCrewmate = vi.fn<(file: Blob) => Promise<Card | null>>()

vi.mock('../../api/cards', async (orig) => {
  const real = await orig<typeof import('../../api/cards')>()
  return { ...real, cardsApi: { ...real.cardsApi, pending: (s: string) => pending(s), importCrewmate: (f: Blob) => importCrewmate(f) } }
})
vi.mock('../../cards/ChangeCard', () => ({
  default: ({ card, remove }: { card: Card; remove: (id: string) => void }) => (
    <div>
      <div data-testid="change-card-stub">{card.id}</div>
      <button type="button" data-testid="change-card-dismiss-stub" onClick={() => remove(card.id)}>x</button>
    </div>
  ),
}))

import CrewmateImportDialog, { CREWMATE_FILE_ACCEPT, importRefusal } from './CrewmateImportDialog'
import { CREWMATE_IMPORT_SLOT } from '../../api/cards'

const card = (over: Partial<Card> = {}): Card => ({
  id: 'cc_1', slot_key: CREWMATE_IMPORT_SLOT, kind: 'crewmate.import', revision: 1, status: 'pending',
  risk: 'normal', title: 'Import', changes: [], editable: ['name'], params: {}, plan: { apply: [], undo: null },
  created_at: 0, expires_at: 0, ...over,
})

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <CrewmateImportDialog open onOpenChange={() => {}} />
    </QueryClientProvider>,
  )
}

describe('CrewmateImportDialog', () => {
  beforeEach(() => {
    pending.mockReset()
    importCrewmate.mockReset()
  })

  it('reads the reserved import slot and reopens a card left pending', async () => {
    pending.mockResolvedValue([card()])
    mount()
    expect(await screen.findByTestId('change-card-stub')).toBeTruthy()
    expect(pending).toHaveBeenCalledWith('owner:crewmate-import')
  })

  it('says that closing keeps a pending import and declining throws it away', async () => {
    pending.mockResolvedValue([card()])
    mount()
    expect((await screen.findByTestId('crewmate-import-keep-hint')).textContent).toMatch(/Decide later keeps this import waiting/)
    expect(screen.getByTestId('crewmate-import-close').textContent).toBe('Decide later')
  })

  it('shows no keep hint once nothing is pending', async () => {
    pending.mockResolvedValue([card({ id: 'cc_partial', status: 'partial' })])
    mount()
    await screen.findByTestId('change-card-stub')
    expect(screen.queryByTestId('crewmate-import-keep-hint')).toBeNull()
  })

  it('offers a picker filtered to .crewmate files', async () => {
    pending.mockResolvedValue([])
    mount()
    const input = screen.getByTestId('crewmate-import-input') as HTMLInputElement
    expect(input.type).toBe('file')
    expect(input.accept).toBe(CREWMATE_FILE_ACCEPT)
    expect(CREWMATE_FILE_ACCEPT).toBe('.crewjam,.crewmate')
  })

  it('sends the picked file and shows the one card the gateway raised', async () => {
    pending.mockResolvedValue([])
    importCrewmate.mockResolvedValue(card({ id: 'cc_new' }))
    mount()
    const file = new File(['PK'], 'Release Captain.crewmate')
    fireEvent.change(screen.getByTestId('crewmate-import-input'), { target: { files: [file] } })
    expect((await screen.findByTestId('change-card-stub')).textContent).toBe('cc_new')
    expect(importCrewmate).toHaveBeenCalledWith(file)
  })

  it('keeps a failed or partial import on screen so its Retry or Undo stays reachable', async () => {
    pending.mockResolvedValue([card({ id: 'cc_partial', status: 'partial' })])
    mount()
    expect((await screen.findByTestId('change-card-stub')).textContent).toBe('cc_partial')
  })

  it('reports a failed read of the import cards and does not open the picker', async () => {
    pending.mockRejectedValue(new Error('the change cards could not be read'))
    const click = vi.spyOn(HTMLInputElement.prototype, 'click')
    mount()
    expect(await screen.findByTestId('crewmate-import-read-error')).toBeTruthy()
    expect(click).not.toHaveBeenCalled()
    click.mockRestore()
  })

  it('a dismissed card does not come back from the just-raised copy', async () => {
    pending.mockResolvedValue([])
    importCrewmate.mockResolvedValue(card({ id: 'cc_new' }))
    mount()
    fireEvent.change(screen.getByTestId('crewmate-import-input'), { target: { files: [new File(['PK'], 'a.crewmate')] } })
    fireEvent.click(await screen.findByTestId('change-card-dismiss-stub'))
    await waitFor(() => expect(screen.queryByTestId('change-card-stub')).toBeNull())
  })

  it('says why a refused file raised no card', async () => {
    pending.mockResolvedValue([])
    importCrewmate.mockRejectedValue({ body: JSON.stringify({ code: 'grant_in_bundle', error: 'x' }) })
    mount()
    fireEvent.change(screen.getByTestId('crewmate-import-input'), { target: { files: [new File(['x'], 'a.crewmate')] } })
    await waitFor(() => expect(screen.getByTestId('crewmate-import-refused')).toBeTruthy())
    expect(screen.queryByTestId('change-card-stub')).toBeNull()
    expect(importRefusal({ body: JSON.stringify({ code: 'grant_in_bundle' }) })).not.toBe(importRefusal({}))
  })
})
