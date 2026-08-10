// First coverage for the Design Critique page — the app shell that owns every
// phase of a critique (new → uploading → scanning → scoping → analyzing →
// report / error), the resume-from-localStorage path, and the History list.
//
// The page's only seam to the outside world is `./api`, so that module is mocked
// and nothing here touches the network. Two things shape the style of this file:
//
//   1. The poll loops sleep on `setTimeout(1500)` and end on EVIDENCE (a finished
//      slot carrying parseable JSON), not on a clock the test can skip. So the
//      whole file runs on fake timers and drives them explicitly with
//      `advanceTimersByTimeAsync` inside `act` — the pattern App.test.tsx and
//      ChannelsPanel.test.tsx already use.
//   2. Interactions go through `fireEvent` rather than `userEvent`, because
//      userEvent's own internal delays would have to be threaded through the same
//      fake clock that is being used to step the poll loop. `fireEvent` keeps each
//      step synchronous and the timer budget of each test explicit.
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import { designCritiqueApi } from '../apps/design-critique/api'
import { HKEY, JOBKEY, LIVEKEY, SLOTSKEY } from '../apps/design-critique/constants'
import type { Ask, HistoryEntry, Report, Scope, SlotData } from '../apps/design-critique/types'

// The single seam between the page and the gateway. `fileUrl` is left real: it is
// a pure string builder and the page renders its output as an <img src>.
vi.mock('../apps/design-critique/api', () => ({
  designCritiqueApi: {
    openSlot: vi.fn(),
    getSlot: vi.fn(),
    send: vi.fn(),
    deleteSlot: vi.fn(),
    uploadFiles: vi.fn(),
  },
  fileUrl: (p: string) => '/api/file-raw?path=' + encodeURIComponent(p),
}))

const mockApi = designCritiqueApi as unknown as {
  openSlot: Mock
  getSlot: Mock
  send: Mock
  deleteSlot: Mock
  uploadFiles: Mock
}

const DesignCritiquePage = (await import('../apps/design-critique/DesignCritiquePage')).default

/** A finished slot whose last assistant message carries `payload` as JSON. The
 *  fence is deliberate — that is how the model actually replies, and
 *  `extractJson` has to strip it. */
function assistantJson(payload: unknown): SlotData {
  return {
    running: false,
    messages: [
      { role: 'user', content: 'critique this' },
      { role: 'assistant', content: '```json\n' + JSON.stringify(payload) + '\n```' },
    ],
  }
}

const imageFile = (name: string): File => new File(['pixels'], name, { type: 'image/png' })

/** One screenshot's critique: no flow, one located finding, so the report renders
 *  a pin on the canvas and a single "What I'd tighten" section. */
const ONE_SCREEN_REPORT: Report = {
  overallRead: 'Clear enough to use, but the two blue buttons fight each other.',
  health: 'Nearly there',
  tally: { major: 1 },
  keep: ['Generous spacing between the fields.'],
  couldNotSee: ['The error states — nothing invalid was reachable.'],
  findings: [{
    severity: 'major',
    title: 'Two primary buttons compete',
    category: 'Hierarchy',
    location: 'Footer action row',
    evidence: 'Save and Publish are both filled in the same blue.',
    fix: 'Demote one of them to a quiet button.',
    rules: ['Nielsen: consistency & standards'],
    box: { x: 0.2, y: 0.6, w: 0.3, h: 0.1 },
  }],
}

/** What STEP 1 replies for a repo: two candidate screens and one observed flow. */
const DISCOVERY = {
  framework: 'React + Vite',
  screens: [
    { id: 'cart', label: 'Cart', ref: 'src/routes/Cart.tsx' },
    { id: 'pay', label: 'Payment', ref: 'src/routes/Pay.tsx' },
  ],
  flows: [{ label: 'Checkout', basis: 'observed', screenIds: ['cart', 'pay'] }],
}

function historyEntry(over: Partial<HistoryEntry> = {}): HistoryEntry {
  return {
    id: 1,
    ts: Date.now(),
    slotKey: 'slot-old',
    screens: [{ step: 1, label: 'Dashboard', url: '/api/file-raw?path=%2Ftmp%2Fold.png' }],
    thumbUrl: '/api/file-raw?path=%2Ftmp%2Fold.png',
    read: 'A tidy dashboard that hides its most useful control.',
    report: {
      overallRead: 'A tidy dashboard that hides its most useful control.',
      tally: { minor: 1 },
      findings: [{ severity: 'minor', title: 'The filter is below the fold', box: null }],
    },
    ...over,
  }
}

/** Step the fake clock and let every promise it releases settle. */
async function tick(ms = 0): Promise<void> {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

/** One poll cycle is a 1500ms sleep followed by a slot read. */
const POLL_MS = 2000

const dropZone = (): HTMLElement =>
  screen.getByRole('button', { name: /Drop screenshots/ })

/** The drop handler sits on the composer, so any child inside it is a valid drop
 *  target. Once something is staged the empty-state tile is replaced by the
 *  staged strip, whose "Add screens" tile is then the child to aim at. */
const dropTarget = (): HTMLElement =>
  screen.queryByRole('button', { name: /Drop screenshots/ })
  ?? screen.getByRole('button', { name: 'Add screens' })

function dropFiles(files: File[]): void {
  fireEvent.drop(dropTarget(), { dataTransfer: { files, types: ['Files'] } })
}

const linkField = (): HTMLElement =>
  screen.getByPlaceholderText(/Figma link · git repo/)

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.useFakeTimers()
  mockApi.openSlot.mockResolvedValue({ key: 'slot-1' })
  mockApi.send.mockResolvedValue(undefined)
  mockApi.deleteSlot.mockResolvedValue(undefined)
  mockApi.uploadFiles.mockResolvedValue({ paths: ['/tmp/shot-1.png'] })
  // Default: the critic has not answered yet, so a poll loop keeps going until a
  // test says otherwise.
  mockApi.getSlot.mockResolvedValue({ running: true, messages: [] })
})

afterEach(() => {
  // Unmount before restoring the clock: a component torn down by the automatic
  // cleanup while its poll promise is mid-flight flips aliveRef and the loop
  // exits, so no pending work escapes into the next test.
  cleanup()
  vi.useRealTimers()
})

describe('Design Critique — first visit', () => {
  it('introduces itself, offers the example, and admits it has no critiques', () => {
    render(<DesignCritiquePage />)

    expect(screen.getByText('Design Critique')).toBeInTheDocument()
    expect(screen.getByText(/read it the way a designer friend would/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /See an example/ })).toBeInTheDocument()
    expect(screen.getByText('Your critiques')).toBeInTheDocument()
    expect(screen.getByText(/No critiques yet/)).toBeInTheDocument()
    // The composer, not a report.
    expect(screen.getByText('What should I critique?')).toBeInTheDocument()
    // Nothing to start over from yet, so the rail offers no "New".
    expect(screen.queryByRole('button', { name: 'New' })).toBeNull()
  })

  it('reaps a slot left behind by an earlier visit but spares a live run', () => {
    localStorage.setItem(SLOTSKEY, JSON.stringify(['stray-1', 'resumable-1', 'live-1']))
    localStorage.setItem(JOBKEY, JSON.stringify({
      'resumable-1': { stage: 'scanning', slotKey: 'resumable-1', kind: 'repo', ts: Date.now() },
    }))
    localStorage.setItem(LIVEKEY, JSON.stringify([{ k: 'live-1', ts: Date.now() }]))

    render(<DesignCritiquePage />)

    expect(mockApi.deleteSlot).toHaveBeenCalledWith('stray-1')
    expect(mockApi.deleteSlot).toHaveBeenCalledTimes(1)
    // Both spared keys survive in the tracked list.
    expect(JSON.parse(localStorage.getItem(SLOTSKEY) || '[]').sort())
      .toEqual(['live-1', 'resumable-1'])
  })
})

describe('Design Critique — the built-in example', () => {
  const openExample = () => {
    render(<DesignCritiquePage />)
    fireEvent.click(screen.getByRole('button', { name: /See an example/ }))
  }

  it('renders the read, the tally, and every report section', () => {
    openExample()

    expect(screen.getByText(/it loses momentum in the middle/)).toBeInTheDocument()
    expect(screen.getByText('Promising, needs work · 4 screens')).toBeInTheDocument()
    // The tally chips, one per non-zero severity, lower-cased.
    expect(screen.getByText('2 major')).toBeInTheDocument()
    expect(screen.getByText('2 minor')).toBeInTheDocument()
    expect(screen.getByText('1 cosmetic')).toBeInTheDocument()
    expect(screen.getByText('What’s working')).toBeInTheDocument()
    // A 4-screen flow, so cross-screen findings get their own section and each
    // step gets a header.
    expect(screen.getByText('Across the flow')).toBeInTheDocument()
    expect(screen.getAllByTitle('Show this screen')).toHaveLength(4)
    expect(screen.getByText('Couldn’t see')).toBeInTheDocument()
  })

  it('moves the canvas and the pins as you walk the filmstrip', () => {
    openExample()

    // Step 1 carries no located finding, so the canvas starts pin-free. Pins are
    // matched on their numbered title, which the rail's finding rows do not carry.
    expect(screen.queryByTitle(/^1\. Required fields/)).toBeNull()
    expect(screen.getByTitle('Step 1 · Cart')).toHaveAttribute('aria-current', 'true')

    fireEvent.click(screen.getByTitle('Step 2 · Shipping'))

    expect(screen.getByTitle('Step 2 · Shipping')).toHaveAttribute('aria-current', 'true')
    expect(screen.getByTitle('Step 1 · Cart')).toHaveAttribute('aria-current', 'false')
    // Step 2's finding has a box, so it becomes a numbered pin on the canvas.
    expect(screen.getByTitle(/^1\. Required fields/)).toBeInTheDocument()
    expect(screen.getByText('● on screen')).toBeInTheDocument()
  })

  it('jumps the canvas to a finding\'s own step when its row is opened', () => {
    openExample()

    // A screen-scoped finding on step 4: opening it must bring step 4 forward.
    fireEvent.click(screen.getByText('The confirmation is a dead end'))

    expect(screen.getByTitle('Step 4 · Confirmation')).toHaveAttribute('aria-current', 'true')
    // And the row is expanded, showing the evidence and the heuristics behind it.
    // The first row is open by default, so this is the SECOND open body.
    expect(screen.getAllByText('What I saw')).toHaveLength(2)
    expect(screen.getByText(/no onward action/)).toBeInTheDocument()
    expect(screen.getByText('Peak-end rule')).toBeInTheDocument()
  })

  it('enlarges the screen and closes the lightbox with Escape', () => {
    openExample()

    fireEvent.click(screen.getByTitle('Enlarge'))
    const box = screen.getByRole('dialog', { name: 'Full size view' })
    expect(box).toBeInTheDocument()
    expect(within(box).getByAltText('full size')).toBeInTheDocument()

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Full size view' })).toBeNull()
  })

  it('closes the lightbox on a click outside the image', () => {
    openExample()

    fireEvent.click(screen.getByTitle('Enlarge'))
    const overlay = screen.getByRole('dialog', { name: 'Full size view' })
      .parentElement as HTMLElement
    fireEvent.click(overlay)
    expect(screen.queryByRole('dialog', { name: 'Full size view' })).toBeNull()
  })

  it('returns to the composer from the report', () => {
    openExample()
    expect(screen.queryByText('What should I critique?')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'New' }))

    expect(screen.getByText('What should I critique?')).toBeInTheDocument()
    expect(screen.queryByText(/it loses momentum in the middle/)).toBeNull()
  })

  it('offers a way to fill the gaps it could not see', () => {
    openExample()

    // "Couldn't see" hands the user straight back to the two inputs that fix it.
    fireEvent.click(screen.getByRole('button', { name: /Point me at a running URL/ }))

    expect(screen.getByText('What should I critique?')).toBeInTheDocument()
    expect(linkField()).toHaveValue('http://localhost:')
  })
})

describe('Design Critique — annotations saved with a critique', () => {
  /** A critique whose read line was annotated in an earlier session. The ask uses
   *  the pre-turns shape a previous build wrote, which showReport has to migrate
   *  rather than crash on. */
  const legacyAsk = [{
    id: 'a1',
    quote: 'buries its save button',
    q: 'Why is that bad?',
    a: 'Because nothing tells you the page has unsaved work.',
  }] as unknown as Ask[]

  const annotated = () => historyEntry({
    read: 'The settings page buries its save button.',
    report: {
      overallRead: 'The settings page buries its save button.',
      tally: { minor: 1 },
      findings: [{ severity: 'minor', title: 'The save button is below the fold', box: null }],
    },
    asks: legacyAsk,
  })

  const openAnnotated = () => {
    localStorage.setItem(HKEY, JSON.stringify([annotated()]))
    render(<DesignCritiquePage />)
    fireEvent.click(screen.getByText('The settings page buries its save button.'))
  }

  it('marks the quoted text and reopens the saved answer, legacy shape included', () => {
    openAnnotated()

    const mark = screen.getByTitle('Your question — click to see the answer')
    expect(mark).toHaveTextContent('buries its save button')

    fireEvent.click(mark)

    expect(screen.getByText('“buries its save button”')).toBeInTheDocument()
    expect(screen.getByText('Why is that bad?')).toBeInTheDocument()
    expect(screen.getByText('Because nothing tells you the page has unsaved work.'))
      .toBeInTheDocument()
  })

  it('asks a follow-up on the same thread and keeps it with the critique', async () => {
    mockApi.getSlot.mockResolvedValue({
      running: false,
      messages: [{ role: 'assistant', content: 'Because the fold hides it on a laptop.' }],
    })
    openAnnotated()
    fireEvent.click(screen.getByTitle('Your question — click to see the answer'))

    const draft = screen.getByPlaceholderText('Ask a follow-up…')
    fireEvent.change(draft, { target: { value: 'How would you fix it?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))

    // The turn is optimistic: the question is on screen before the answer lands.
    expect(screen.getByText('How would you fix it?')).toBeInTheDocument()
    expect(screen.getByText('Thinking…')).toBeInTheDocument()

    // Two text polls at 1200ms: one for the context turn, one for the answer.
    await tick(4000)
    expect(screen.getByText('Because the fold hides it on a laptop.')).toBeInTheDocument()
    // Follow-ups share ONE slot so the thread keeps its context.
    expect(mockApi.openSlot).toHaveBeenCalledTimes(1)
    // And the answer is written back onto this critique's history entry.
    const saved = JSON.parse(localStorage.getItem(HKEY) || '[]')
    expect(saved[0].asks[0].turns).toHaveLength(2)
    expect(saved[0].asks[0].turns[1].a).toBe('Because the fold hides it on a laptop.')
  })

  it('reports a failed follow-up on the turn itself', async () => {
    mockApi.send.mockRejectedValue(new Error('the critic is gone'))
    openAnnotated()
    fireEvent.click(screen.getByTitle('Your question — click to see the answer'))

    fireEvent.change(screen.getByPlaceholderText('Ask a follow-up…'), {
      target: { value: 'Still there?' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    await tick()

    expect(screen.getByText('the critic is gone')).toBeInTheDocument()
    // The gate is released, so a second question is still possible.
    expect(screen.getByPlaceholderText('Ask a follow-up…')).not.toBeDisabled()
  })

  it('removes an annotation and unmarks the text', () => {
    openAnnotated()
    fireEvent.click(screen.getByTitle('Your question — click to see the answer'))

    fireEvent.click(screen.getByRole('button', { name: 'Remove this annotation' }))

    expect(screen.queryByTitle('Your question — click to see the answer')).toBeNull()
    expect(screen.queryByText('Why is that bad?')).toBeNull()
    expect(JSON.parse(localStorage.getItem(HKEY) || '[]')[0].asks).toEqual([])
  })
})

describe('Design Critique — staging screenshots', () => {
  it('stages dropped images in order and names the flow', () => {
    render(<DesignCritiquePage />)

    dropFiles([imageFile('cart.png'), imageFile('pay.png')])

    expect(screen.getByText(/2 screens · this order is the flow order/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Critique this flow · 2 screens/ }))
      .toBeInTheDocument()
    expect(screen.getByAltText('cart.png')).toBeInTheDocument()
    expect(screen.getByAltText('pay.png')).toBeInTheDocument()
  })

  it('refuses a drop that carries no images', () => {
    render(<DesignCritiquePage />)

    dropFiles([new File(['notes'], 'notes.txt', { type: 'text/plain' })])

    expect(screen.getByText('Those weren’t image files.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Critique$/ })).toBeDisabled()
  })

  it('caps staging at 20 screens and says so both times', () => {
    render(<DesignCritiquePage />)

    dropFiles(Array.from({ length: 21 }, (_, i) => imageFile('s' + i + '.png')))
    expect(screen.getByText('Only added the first 20 — the limit is 20 screens.'))
      .toBeInTheDocument()

    dropFiles([imageFile('one-more.png')])
    expect(screen.getByText('That’s the limit of 20 screens.')).toBeInTheDocument()
    expect(screen.queryByAltText('one-more.png')).toBeNull()
  })

  it('removes a staged screen and drops back to a single-screen critique', () => {
    render(<DesignCritiquePage />)
    dropFiles([imageFile('cart.png'), imageFile('pay.png')])

    fireEvent.click(screen.getByRole('button', { name: 'Remove pay.png' }))

    expect(screen.queryByAltText('pay.png')).toBeNull()
    expect(screen.getByRole('button', { name: /Critique this screen/ })).toBeInTheDocument()
  })

  it('reorders staged screens, because the order IS the flow order', () => {
    render(<DesignCritiquePage />)
    dropFiles([imageFile('cart.png'), imageFile('pay.png')])

    const names = () => screen.getAllByRole('img').map(i => i.getAttribute('alt'))
    expect(names()).toEqual(['cart.png', 'pay.png'])

    // "Move later" on the first tile: only the first tile's control is enabled at
    // index 0's left edge, so pick the later-mover belonging to that tile.
    const firstTile = screen.getByAltText('cart.png').closest('div')
      ?.parentElement as HTMLElement
    fireEvent.click(within(firstTile).getByRole('button', { name: 'Move later' }))

    expect(names()).toEqual(['pay.png', 'cart.png'])
  })

  it('clears every staged screen at once', () => {
    render(<DesignCritiquePage />)
    dropFiles([imageFile('cart.png'), imageFile('pay.png')])

    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }))

    expect(screen.queryByAltText('cart.png')).toBeNull()
    expect(screen.getByRole('button', { name: /Drop screenshots/ })).toBeInTheDocument()
  })

  it('highlights the drop zone only while a drag is over it', () => {
    render(<DesignCritiquePage />)
    const zone = dropZone()
    expect(zone.style.borderColor).not.toContain('--accent')

    fireEvent.dragOver(zone)
    expect(dropZone().style.borderColor).toContain('--accent')

    fireEvent.dragLeave(dropZone())
    expect(dropZone().style.borderColor).not.toContain('--accent')
  })

  it('accepts images chosen through the file picker', () => {
    const { container } = render(<DesignCritiquePage />)
    const picker = container.querySelector('input[type="file"]') as HTMLInputElement

    // `files` is read-only on the element, so it is installed directly rather
    // than through fireEvent's target shorthand.
    Object.defineProperty(picker, 'files', { value: [imageFile('picked.png')], configurable: true })
    fireEvent.change(picker)

    expect(screen.getByAltText('picked.png')).toBeInTheDocument()
  })
})

describe('Design Critique — critiquing screenshots', () => {
  it('uploads, waits on real evidence, then renders and remembers the critique', async () => {
    mockApi.getSlot.mockResolvedValue(assistantJson(ONE_SCREEN_REPORT))
    render(<DesignCritiquePage />)
    dropFiles([imageFile('cart.png')])

    fireEvent.click(screen.getByRole('button', { name: /Critique this screen/ }))

    // Upload first, and the waiting screen says which stage it is in.
    await tick()
    expect(mockApi.uploadFiles).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Reading your screen')).toBeInTheDocument()
    expect(screen.getByText('Reading your design…')).toBeInTheDocument()
    expect(mockApi.send).toHaveBeenCalledWith('slot-1', expect.stringContaining('/tmp/shot-1.png'))

    // One poll cycle later the critic has answered.
    await tick(POLL_MS)
    expect(screen.getByText(/the two blue buttons fight each other/)).toBeInTheDocument()
    expect(screen.getByText('1 major')).toBeInTheDocument()
    expect(screen.getByText('What I\'d tighten')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /1\. Two primary buttons compete/ }))
      .toBeInTheDocument()

    // The finished run is written to History and its slot released.
    const saved = JSON.parse(localStorage.getItem(HKEY) || '[]')
    expect(saved).toHaveLength(1)
    expect(saved[0].read).toContain('the two blue buttons fight each other')
    expect(mockApi.deleteSlot).toHaveBeenCalledWith('slot-1')
    expect(localStorage.getItem(JOBKEY)).toBeNull()
  })

  it('surfaces an upload failure instead of waiting for ever', async () => {
    mockApi.uploadFiles.mockRejectedValue(new Error('upload failed (413)'))
    render(<DesignCritiquePage />)
    dropFiles([imageFile('cart.png')])

    fireEvent.click(screen.getByRole('button', { name: /Critique this screen/ }))
    await tick()

    expect(screen.getByText('upload failed (413)')).toBeInTheDocument()
    // No slot was ever opened, so nothing needs releasing.
    expect(mockApi.openSlot).not.toHaveBeenCalled()
  })

  it('says so when the critic replies with something unreadable', async () => {
    mockApi.getSlot.mockResolvedValue({
      running: false, messages: [{ role: 'assistant', content: 'I had a look and it seems fine.' }],
    })
    render(<DesignCritiquePage />)
    dropFiles([imageFile('cart.png')])

    fireEvent.click(screen.getByRole('button', { name: /Critique this screen/ }))
    await tick(POLL_MS)

    expect(screen.getByText('The critic replied but not in a readable format.'))
      .toBeInTheDocument()
  })

  it('cancels a run, releases its slot, and returns to the composer', async () => {
    render(<DesignCritiquePage />)
    dropFiles([imageFile('cart.png')])
    fireEvent.click(screen.getByRole('button', { name: /Critique this screen/ }))
    await tick()

    fireEvent.click(screen.getByRole('button', { name: /Cancel this run/ }))

    expect(screen.getByText('What should I critique?')).toBeInTheDocument()
    expect(mockApi.deleteSlot).toHaveBeenCalledWith('slot-1')
    expect(localStorage.getItem(JOBKEY)).toBeNull()
  })

  it('gives up on a slot that has gone missing rather than polling for ever', async () => {
    mockApi.getSlot.mockRejectedValue(new Error('404'))
    render(<DesignCritiquePage />)
    dropFiles([imageFile('cart.png')])
    fireEvent.click(screen.getByRole('button', { name: /Critique this screen/ }))

    // Eight consecutive misses is the give-up threshold; one miss is not.
    await tick(POLL_MS)
    expect(screen.getByText('Reading your screen')).toBeInTheDocument()

    await tick(8 * 1500)
    expect(screen.getByText('That run is no longer available — start a new critique.'))
      .toBeInTheDocument()
  })

  it('keeps a run that outlives the backstop instead of calling it failed', async () => {
    // The slot answers, but never finishes. Past HARD_CAP_MS the page stops
    // watching WITHOUT destroying the run — it is resumable on the next visit.
    mockApi.getSlot.mockResolvedValue({ running: true, messages: [] })
    render(<DesignCritiquePage />)
    dropFiles([imageFile('cart.png')])
    fireEvent.click(screen.getByRole('button', { name: /Critique this screen/ }))

    await tick(15 * 60 * 1000 + 2000)

    expect(screen.getByText(/Still working on this one/)).toBeInTheDocument()
    // Deliberately NOT torn down: the slot and its job record survive.
    expect(mockApi.deleteSlot).not.toHaveBeenCalled()
    expect(localStorage.getItem(JOBKEY)).not.toBeNull()
  })

  it('keeps a run alive in the background when a new critique is started', async () => {
    mockApi.getSlot.mockResolvedValue({ running: true, messages: [] })
    render(<DesignCritiquePage />)
    dropFiles([imageFile('cart.png')])
    fireEvent.click(screen.getByRole('button', { name: /Critique this screen/ }))
    await tick()

    // "New" is explicitly NOT a cancel: the run keeps going and earns a row.
    fireEvent.click(screen.getByRole('button', { name: 'New' }))
    expect(screen.getByText('What should I critique?')).toBeInTheDocument()
    expect(screen.getByTitle('A critique is still running — click to watch it'))
      .toBeInTheDocument()
    expect(mockApi.deleteSlot).not.toHaveBeenCalled()

    // When it lands it fills its own row in place and announces itself instead of
    // hijacking the screen the user moved to.
    mockApi.getSlot.mockResolvedValue(assistantJson(ONE_SCREEN_REPORT))
    await tick(POLL_MS)
    expect(screen.getByText('What should I critique?')).toBeInTheDocument()
    const chip = screen.getByRole('button', { name: /critique ready/ })
    const saved = JSON.parse(localStorage.getItem(HKEY) || '[]')
    expect(saved).toHaveLength(1)
    expect(saved[0].pending).toBe(false)

    fireEvent.click(chip)
    expect(screen.getByText(/the two blue buttons fight each other/)).toBeInTheDocument()
  })
})

describe('Design Critique — critiquing a reference', () => {
  const start = (text: string) => {
    render(<DesignCritiquePage />)
    fireEvent.change(linkField(), { target: { value: text } })
    fireEvent.keyDown(linkField(), { key: 'Enter' })
  }

  it('rejects text that is not a design reference at all', () => {
    start('some notes about the design')

    expect(screen.getByText(/I couldn’t tell what that is/)).toBeInTheDocument()
    expect(mockApi.openSlot).not.toHaveBeenCalled()
  })

  it('discovers the screens, then critiques only the picked flow', async () => {
    mockApi.getSlot.mockResolvedValue(assistantJson(DISCOVERY))
    start('https://github.com/acme/widgets')

    await tick()
    expect(screen.getByText('Looking for screens to audit')).toBeInTheDocument()

    await tick(POLL_MS)
    expect(screen.getByText('What should I audit?')).toBeInTheDocument()
    expect(screen.getByText(/2 screens found · 2 I can render/)).toBeInTheDocument()
    expect(screen.getByText('React + Vite')).toBeInTheDocument()
    // The observed flow presets the pick, in its own order.
    expect(screen.getAllByRole('checkbox', { checked: true })).toHaveLength(2)

    // The brief travels with the scoped run, and the same slot is reused.
    fireEvent.change(screen.getByPlaceholderText(/who is it for/), {
      target: { value: 'first-time buyers' },
    })
    mockApi.getSlot.mockResolvedValue(assistantJson(ONE_SCREEN_REPORT))
    fireEvent.click(screen.getByRole('button', { name: /Critique this flow · 2 screens/ }))

    await tick(POLL_MS)
    expect(screen.getByText(/the two blue buttons fight each other/)).toBeInTheDocument()
    expect(mockApi.openSlot).toHaveBeenCalledTimes(1)
    expect(mockApi.send).toHaveBeenLastCalledWith('slot-1', expect.stringContaining('first-time buyers'))
  })

  it('narrows the pick to one screen and says what it will do', async () => {
    mockApi.getSlot.mockResolvedValue(assistantJson(DISCOVERY))
    start('https://github.com/acme/widgets')
    await tick(POLL_MS)

    fireEvent.click(screen.getByRole('checkbox', { name: /Cart/ }))

    expect(screen.getByRole('button', { name: /Critique this screen/ })).toBeInTheDocument()
    expect(screen.getByText(/Click a screen to pick it/)).toBeInTheDocument()

    // The suggested flow puts the whole set back in its own order.
    fireEvent.click(screen.getByText('Checkout'))
    expect(screen.getAllByRole('checkbox', { checked: true })).toHaveLength(2)
  })

  it('reorders the picked screens, since the order is the walk order', async () => {
    mockApi.getSlot.mockResolvedValue(assistantJson(DISCOVERY))
    start('https://github.com/acme/widgets')
    await tick(POLL_MS)

    const cart = screen.getByRole('checkbox', { name: /Cart/ })
    // Cart is picked first, so its "move later" swaps it with Payment. The rows
    // keep their discovery order; it is the ORDINAL badge that carries the walk
    // order, so that is what has to move.
    expect(cart.textContent).toContain('1')
    fireEvent.click(within(cart).getByRole('button', { name: 'Move later' }))
    expect(cart.textContent).toContain('2')

    // Dragging one picked row onto another reorders it back.
    fireEvent.dragStart(cart, { dataTransfer: { effectAllowed: 'move' } })
    fireEvent.dragOver(screen.getByRole('checkbox', { name: /Payment/ }))
    fireEvent.drop(cart)
    expect(cart.textContent).toContain('1')
  })

  it('explains a blocked reference and offers the access steps', async () => {
    mockApi.getSlot.mockResolvedValue(assistantJson({
      blocked: { reason: 'no-access', detail: 'HTTP 404 from github.com' },
    }))
    start('https://github.com/acme/private-thing')
    await tick(POLL_MS)

    expect(screen.getByText('I couldn’t get in')).toBeInTheDocument()
    expect(screen.getByText(/It’s either private/)).toBeInTheDocument()
    expect(screen.getByText('HTTP 404 from github.com')).toBeInTheDocument()
    // The slot is released as soon as the run is known to be over.
    expect(mockApi.deleteSlot).toHaveBeenCalledWith('slot-1')

    fireEvent.click(screen.getByRole('button', { name: 'Fix my access' }))
    expect(screen.getByText(/Git access is set up per machine/)).toBeInTheDocument()
    expect(screen.getByText(/gh auth login/)).toBeInTheDocument()
  })

  it('says it got in but found nothing renderable, using the critic\'s own note', async () => {
    mockApi.getSlot.mockResolvedValue(assistantJson({
      screens: [], cannotSee: ['Every route needs a running server.'],
    }))
    start('https://github.com/acme/widgets')
    await tick(POLL_MS)

    expect(screen.getByText('Every route needs a running server.')).toBeInTheDocument()
  })
})

describe('Design Critique — resuming and History', () => {
  it('resumes a run that was left at the scoping step', () => {
    const scope: Scope = {
      framework: 'Next.js',
      screens: [{ id: 'home', label: 'Home' }, { id: 'about', label: 'About' }],
      flows: [],
    }
    localStorage.setItem(JOBKEY, JSON.stringify({
      'slot-resume': {
        stage: 'scoping', slotKey: 'slot-resume', kind: 'repo', ts: Date.now(),
        scope, picked: ['home'], refBrief: 'returning visitors',
      },
    }))

    render(<DesignCritiquePage />)

    expect(screen.getByText('What should I audit?')).toBeInTheDocument()
    expect(screen.getByText('Next.js')).toBeInTheDocument()
    expect(screen.getByDisplayValue('returning visitors')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Critique this screen/ })).toBeInTheDocument()
  })

  it('resumes an analyzing run and collects its report', async () => {
    mockApi.getSlot.mockResolvedValue(assistantJson(ONE_SCREEN_REPORT))
    localStorage.setItem(JOBKEY, JSON.stringify({
      'slot-resume': {
        stage: 'analyzing', slotKey: 'slot-resume', ts: Date.now(),
        screens: [{ step: 1, label: 'Screen 1', url: '/api/file-raw?path=%2Ftmp%2Fa.png' }],
      },
    }))

    render(<DesignCritiquePage />)
    expect(screen.getByText('Reading your screen')).toBeInTheDocument()

    await tick(POLL_MS)
    expect(screen.getByText(/the two blue buttons fight each other/)).toBeInTheDocument()
  })

  it('lists a finished critique and reopens it from the rail', () => {
    localStorage.setItem(HKEY, JSON.stringify([historyEntry()]))
    render(<DesignCritiquePage />)

    expect(screen.getByText('A tidy dashboard that hides its most useful control.'))
      .toBeInTheDocument()

    fireEvent.click(screen.getByText('A tidy dashboard that hides its most useful control.'))

    expect(screen.getByText('The filter is below the fold')).toBeInTheDocument()
    expect(screen.getByText('1 minor')).toBeInTheDocument()
  })

  it('shows a backgrounded run as still running and re-attaches to it', () => {
    localStorage.setItem(HKEY, JSON.stringify([
      historyEntry({ slotKey: 'slot-bg', read: '', report: null, pending: true }),
    ]))
    render(<DesignCritiquePage />)

    const row = screen.getByTitle('A critique is still running — click to watch it')
    expect(row).toHaveAttribute('aria-busy', 'true')
    expect(within(row).getByText('running')).toBeInTheDocument()

    fireEvent.click(row)

    // Watching it puts the waiting screen back, driven by a fresh poller.
    expect(screen.getByText('Reading your screen')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Cancel this run/ })).toBeInTheDocument()
  })

  it('switches between critiques from the History menu', () => {
    localStorage.setItem(HKEY, JSON.stringify([
      historyEntry(),
      historyEntry({
        id: 2, slotKey: 'slot-older', read: 'The settings page buries its save button.',
        report: { overallRead: 'The settings page buries its save button.', findings: [] },
      }),
    ]))
    render(<DesignCritiquePage />)
    fireEvent.click(screen.getByText('A tidy dashboard that hides its most useful control.'))

    fireEvent.click(screen.getByRole('button', { name: /History \(2\)/ }))
    fireEvent.click(screen.getByText('The settings page buries its save button.'))

    expect(screen.getByText('The settings page buries its save button.')).toBeInTheDocument()
    expect(screen.getByText('nothing major')).toBeInTheDocument()
  })
})
