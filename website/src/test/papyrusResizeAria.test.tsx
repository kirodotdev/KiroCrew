/**
 * The three resize grips as ARIA window-splitters: the position each one reports
 * must lie inside the range it advertises.
 *
 * This is not decoration. `ResizeHandle` is the splitter widget, so a screen
 * reader announces `aria-valuenow` against `aria-valuemin`/`aria-valuemax`
 * verbatim — a position outside the range is read out as-is, and two Papyrus
 * grips could produce one:
 *
 *  - the PDF grip, because its ceiling is a share of the window while its
 *    starting width was a flat constant, so a window between the two advertised
 *    a maximum below the width being rendered;
 *  - the file-tree grip, because the collapsed rail is 28px and sits outside the
 *    [120, 420] range a drag is clamped to.
 *
 * Both are invisible to a sighted user, which is exactly why they need a test.
 * The same mocks as `PapyrusPageCoverage` apply for the same reasons (Monaco has
 * no accessible input under jsdom, `PdfPreview` fetches a blob URL, and
 * `CoAuthorPanel` mounts the whole ChatPage).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PapyrusPage from '../apps/papyrus/PapyrusPage'
import { createTestStore, renderWithProviders } from './helpers'
import { papyrusApi } from '../apps/papyrus/api'
import {
  CHAT_OPEN_KEY, CHAT_WIDTH_KEY, DEFAULT_PDF_WIDTH, DEFAULT_TREE_WIDTH,
  LAST_PROJECT_KEY, MIN_CHAT_WIDTH, MIN_EDITOR_WIDTH,
} from '../apps/papyrus/lib'

vi.mock('../apps/papyrus/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/papyrus/api')>()),
  papyrusApi: {
    health: vi.fn(),
    listProjects: vi.fn(),
    getProject: vi.fn(),
    listFiles: vi.fn(),
    readFile: vi.fn(),
    saveFile: vi.fn(),
    compile: vi.fn(),
    gitStatus: vi.fn(),
  },
}))

vi.mock('../apps/papyrus/PapyrusEditor', async () => {
  const { forwardRef, useImperativeHandle } = await import('react')
  return {
    default: forwardRef<{ jumpToLine: (line: number) => void; focus: () => void }, {
      value: string
      onChange: (v: string) => void
    }>(({ value, onChange }, ref) => {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }))
      return <textarea aria-label="editor" value={value} onChange={e => onChange(e.target.value)} />
    }),
  }
})

vi.mock('../apps/papyrus/PdfPreview', () => ({ default: () => <div data-testid="pdf" /> }))
vi.mock('../apps/papyrus/CoAuthorPanel', () => ({
  default: () => <div data-testid="co-author-panel" />,
}))

const api = vi.mocked(papyrusApi)
const PROJECT = 'thesis'
const MAIN = 'main.tex'

/** Every splitter on screen, with the three ARIA numbers it is announcing. */
function splitters() {
  return screen.getAllByTestId('resize-handle').map(el => ({
    label: el.getAttribute('aria-label') ?? '',
    now: Number(el.getAttribute('aria-valuenow')),
    min: Number(el.getAttribute('aria-valuemin')),
    max: Number(el.getAttribute('aria-valuemax')),
  }))
}

async function openWorkspace() {
  localStorage.setItem(LAST_PROJECT_KEY, PROJECT)
  renderWithProviders(<PapyrusPage />, {
    store: createTestStore(),
    queryDefaults: { staleTime: 30_000 },
  })
  const user = userEvent.setup()
  await screen.findByTestId('papyrus-workspace')
  await screen.findByLabelText('editor')
  return user
}

beforeEach(() => {
  localStorage.clear()
  api.health.mockResolvedValue({ ok: true, pdflatex: true })
  api.listProjects.mockResolvedValue({ projects: [{ name: PROJECT, files: 1 }] })
  api.getProject.mockResolvedValue({
    name: PROJECT, main_file: MAIN, files: [MAIN], content: '\\documentclass{article}',
    pdf_url: null, log: null,
  })
  api.readFile.mockResolvedValue({ content: '\\documentclass{article}' })
  api.gitStatus.mockResolvedValue({ is_repo: false })
})

describe('papyrus resize grips report a position inside the range they advertise', () => {
  it('holds for every grip on first load, with no stored width to fall back from', async () => {
    // jsdom reports a 1024px window, where the preview's ceiling (512) is BELOW
    // the 520 starting width the column used to take unconditionally — so this
    // assertion fails on an unbounded default rather than passing either way.
    await openWorkspace()
    const grips = splitters()
    expect(grips.length, 'the tree and preview grips are both mounted').toBeGreaterThanOrEqual(2)
    for (const grip of grips) {
      expect(grip.now, `${grip.label} reports below its own minimum`)
        .toBeGreaterThanOrEqual(grip.min)
      expect(grip.now, `${grip.label} reports above its own maximum`)
        .toBeLessThanOrEqual(grip.max)
    }
  })

  it('holds for the file-tree grip once the tree is collapsed to its rail', async () => {
    const user = await openWorkspace()
    const tree = screen.getAllByTestId('resize-handle')[0]
    const before = Number(tree.getAttribute('aria-valuenow'))

    // Shift+ArrowLeft is the coarse step (64px): from 176 it lands on 112, under
    // the 120 minimum, which is the drag's collapse condition.
    tree.focus()
    await user.keyboard('{Shift>}{ArrowLeft}{/Shift}')

    const collapsed = splitters()[0]
    expect(collapsed.now, 'the step must have moved the column').not.toBe(before)
    expect(collapsed.now, 'the 28px rail must not be announced below the minimum')
      .toBeGreaterThanOrEqual(collapsed.min)
    expect(collapsed.now).toBeLessThanOrEqual(collapsed.max)
  })
})

/** The width actually applied to the co-author column, in px. */
function chatBoxWidth(): number {
  const box = screen.getByTestId('co-author-panel').parentElement
  return parseFloat(box?.style.width ?? '')
}

const JSDOM_VIEWPORT = 1024
const setViewport = (value: number) => {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value })
}

describe('the co-author panel yields width instead of the editor', () => {
  afterEach(() => { setViewport(JSDOM_VIEWPORT) })

  it('opens narrower than its stored width when the room does not hold it', async () => {
    // 1600px: the tree and preview take 176 + 520 at their defaults, so 624px is
    // all the panel may have if the editor keeps its 280px floor. A stored 700
    // would have rendered whole — the panel is restored at mount here, which is
    // the path a drag-time clamp alone would miss.
    setViewport(1600)
    localStorage.setItem(CHAT_OPEN_KEY, '1')
    localStorage.setItem(CHAT_WIDTH_KEY, '700')
    await openWorkspace()

    const width = chatBoxWidth()
    expect(width).toBe(624)
    expect(1600 - DEFAULT_TREE_WIDTH - DEFAULT_PDF_WIDTH - width)
      .toBeGreaterThanOrEqual(MIN_EDITOR_WIDTH)
  })

  it('keeps a stored width the room can hold', async () => {
    // Same window, a width that fits: yielding must not become a permanent
    // haircut, or the preference is lost rather than deferred.
    setViewport(1600)
    localStorage.setItem(CHAT_OPEN_KEY, '1')
    localStorage.setItem(CHAT_WIDTH_KEY, '500')
    await openWorkspace()

    expect(chatBoxWidth()).toBe(500)
  })

  it('stops at its own minimum on a window too narrow to buy the floor back', async () => {
    // jsdom's 1024px: 176 + 512 + 280 already exceeds what a 280px editor would
    // leave, so the panel goes to its minimum and no further.
    localStorage.setItem(CHAT_OPEN_KEY, '1')
    localStorage.setItem(CHAT_WIDTH_KEY, '420')
    await openWorkspace()

    expect(chatBoxWidth()).toBe(MIN_CHAT_WIDTH)
  })

  it('announces a position inside the range the drag enforces', async () => {
    // The ceiling moved, so the grip has to move with it: `aria-valuemax` is the
    // room, not the flat 720 the column could once claim.
    setViewport(1600)
    localStorage.setItem(CHAT_OPEN_KEY, '1')
    localStorage.setItem(CHAT_WIDTH_KEY, '700')
    await openWorkspace()

    const grips = splitters()
    const chat = grips[grips.length - 1]
    expect(chat.max).toBe(624)
    expect(chat.now).toBe(624)
    expect(chat.now).toBeGreaterThanOrEqual(chat.min)
    expect(chat.now).toBeLessThanOrEqual(chat.max)
  })
})
