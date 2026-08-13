/**
 * Mochi chat panel — first tests for `apps/mochi/src/renderer/ChatPanel.tsx`.
 *
 * The panel is the pet's whole conversation surface and had no test at all, so
 * these cover the behaviours a user can actually reach: the header and its
 * toggles, history load, the composer (send, failure recovery, slash commands),
 * the destructive confirmations behind the context menu, edit-and-resend, and
 * the inline approval card — including the two paths that must never fabricate
 * a security verdict (a failed POST, and a resolution that happened on another
 * surface).
 *
 * The panel talks to the backend exclusively through `mochiApi`, so that module
 * is the single mock. Its `on*` subscribers are captured into an emitter table
 * so a test can push a real backend frame (`chat:message`, `approval`,
 * `slots:update`, …) and assert what the panel renders in response.
 *
 * No test depends on an animation frame landing: the streaming channel throttles
 * through `requestAnimationFrame`, so the committed-message channel is used to
 * drive turn state instead.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

type AnyFn = (...args: never[]) => unknown

/** Captured `on*` subscribers, keyed by the api method that registered them. */
const subscribers = new Map<string, Set<AnyFn>>()

/** Build an `on*` implementation that records its callback and returns an unsubscribe. */
function subscribe(channel: string) {
  return (cb: AnyFn) => {
    const set = subscribers.get(channel) ?? new Set<AnyFn>()
    set.add(cb)
    subscribers.set(channel, set)
    return () => { set.delete(cb) }
  }
}

/** Push a backend frame to whatever the panel registered on `channel`. */
function emit(channel: string, ...args: unknown[]): void {
  for (const cb of Array.from(subscribers.get(channel) ?? [])) {
    ;(cb as (...a: unknown[]) => unknown)(...args)
  }
}

/** Chat history handed back by `getChatHistory`; set per test before render. */
let history: unknown[] = []
/** Initial backend reachability, so the offline banner can be exercised. */
let backendOnline = true

const sendMessage = vi.fn(async () => undefined)
const editResend = vi.fn(async () => ({ ok: true }))
const newSession = vi.fn(async () => undefined)
const respondApproval = vi.fn(async () => undefined as unknown)
const stopGeneration = vi.fn(async () => undefined)
const retryConnect = vi.fn(async () => ({ ok: true } as { ok: boolean; message?: string }))
const resetMochi = vi.fn(async () => undefined)
const deleteHistory = vi.fn(async () => undefined)
const closeChat = vi.fn()
const openSettings = vi.fn()
const galleryOpen = vi.fn()
const openDashboard = vi.fn()
const openLightbox = vi.fn()
const previewFile = vi.fn()
const revealFile = vi.fn()
const markPinnedSeen = vi.fn()
const unpinFile = vi.fn()
const openExternal = vi.fn()
/** Local image bytes, so an inline image can render without touching disk. */
const readLocalImage = vi.fn(async (_path: string): Promise<string | null> => null)

/**
 * Whether the panel believes it runs inside the Electron shell. The reveal
 * button delegates to the shell bridge, so the panel withholds it in a plain
 * browser tab; most tests here exercise the shell surface, hence `true`.
 * Read through a getter so a test can flip it without a module reset.
 */
let electronShell = true
vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  get isElectron() { return electronShell },
}))

vi.mock('../apps/mochi/src/mochiApi', () => ({
  api: {
    getMochiConfig: async () => ({ petName: 'Mochi', theme: 'mocha' }),
    getConfig: async () => ({
      shortcuts: {
        toggleWindow: 'CommandOrControl+Shift+M',
        screenCapture: 'CommandOrControl+Shift+X',
        hideAll: 'CommandOrControl+Shift+H',
      },
    }),
    getPetStateInfo: async () => ({ state: 'idle', mood: 'happy' }),
    getChatHistory: async () => history,
    getBackendStatus: async () => backendOnline,
    onStateChange: subscribe('onStateChange'),
    onMood: subscribe('onMood'),
    onPeeking: subscribe('onPeeking'),
    onConfigUpdated: subscribe('onConfigUpdated'),
    onChatChunk: subscribe('onChatChunk'),
    onChatDone: subscribe('onChatDone'),
    onChatMessage: subscribe('onChatMessage'),
    onBackendStatus: subscribe('onBackendStatus'),
    onBackendSwitching: subscribe('onBackendSwitching'),
    onSlotsUpdate: subscribe('onSlotsUpdate'),
    onCaptureDone: subscribe('onCaptureDone'),
    onApprovalRequest: subscribe('onApprovalRequest'),
    onApprovalResolvedExternal: subscribe('onApprovalResolvedExternal'),
    onThemeChanged: subscribe('onThemeChanged'),
    onContextUsage: subscribe('onContextUsage'),
    sendMessage,
    editResend,
    newSession,
    respondApproval,
    stopGeneration,
    retryConnect,
    resetMochi,
    deleteHistory,
    closeChat,
    openSettings,
    galleryOpen,
    openDashboard,
    openLightbox,
    previewFile,
    revealFile,
    markPinnedSeen,
    unpinFile,
    openExternal,
    readLocalImage,
  },
}))

const { ChatPanel, PinnedSidePanel, parseApproval, externalApprovalApproved } =
  await import('../apps/mochi/src/renderer/ChatPanel')

beforeEach(() => {
  vi.clearAllMocks()
  subscribers.clear()
  history = []
  backendOnline = true
  electronShell = true
  sendMessage.mockResolvedValue(undefined)
  editResend.mockResolvedValue({ ok: true })
  respondApproval.mockResolvedValue(undefined)
  retryConnect.mockResolvedValue({ ok: true })
  readLocalImage.mockResolvedValue(null)
})

/** Render the panel and wait until the mount-time config reads have settled. */
async function renderPanel(props: Partial<React.ComponentProps<typeof ChatPanel>> = {}) {
  const view = render(<ChatPanel {...props} />)
  // The header state comes from `getPetStateInfo`; waiting on it means every
  // mount effect has flushed before a test starts interacting.
  await screen.findByText(/Idle/)
  return view
}

/** The panel's composer. */
function composer(): HTMLTextAreaElement {
  return screen.getByPlaceholderText(/./) as HTMLTextAreaElement
}

/**
 * The disconnected banner is deferred 1.5s so a brief socket blip does not
 * flash it, which outlasts the default find window.
 */
function findOfflineBanner() {
  return screen.findByText('Kiro Crew disconnected', {}, { timeout: 5000 })
}

/** An approval frame as the gateway pushes it. */
function approvalFrame(extra: Record<string, unknown> = {}) {
  return { id: 'req-1', tool: 'execute_bash', toolInput: 'ls -la', ...extra }
}

describe('parseApproval', () => {
  it('returns the payload when id and tool are both strings', () => {
    expect(parseApproval('{"id":"a","tool":"execute_bash"}')).toEqual({
      id: 'a',
      tool: 'execute_bash',
    })
  })

  it('rejects a payload that parses to a non-object', () => {
    // `__approval__"hi"` is valid JSON; reading `.tool` off a string would
    // render `undefined` into the bubble instead of what the user typed.
    expect(parseApproval('"hi"')).toBeNull()
    expect(parseApproval('null')).toBeNull()
  })

  it('rejects an object missing the id or tool field', () => {
    expect(parseApproval('{"tool":"execute_bash"}')).toBeNull()
    expect(parseApproval('{"id":"a"}')).toBeNull()
    expect(parseApproval('{"id":1,"tool":"x"}')).toBeNull()
  })

  it('returns null instead of throwing on malformed JSON', () => {
    expect(parseApproval('not json at all')).toBeNull()
  })
})

describe('externalApprovalApproved', () => {
  it('is true only for an explicit approved flag', () => {
    expect(externalApprovalApproved({ approved: true })).toBe(true)
  })

  it('treats a reject, a missing flag, and a missing frame as not approved', () => {
    expect(externalApprovalApproved({ approved: false })).toBe(false)
    expect(externalApprovalApproved({})).toBe(false)
    expect(externalApprovalApproved(undefined)).toBe(false)
    // A truthy non-boolean must not read as approved either.
    expect(externalApprovalApproved({ approved: 'yes' })).toBe(false)
  })
})

describe('PinnedSidePanel', () => {
  const pin = (path: string, label = '') => ({ path, label, pinnedAt: 1 })

  it('renders nothing when not visible', () => {
    const { container } = render(
      <PinnedSidePanel pins={[pin('/home/u/a.ts')]} updatedPaths={new Set()}
        deletedPaths={new Set()} visible={false} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('names the pet in the empty hint', () => {
    render(
      <PinnedSidePanel pins={[]} updatedPaths={new Set()} deletedPaths={new Set()}
        visible petName="Kiro" />,
    )
    expect(screen.getByText('Ask Kiro to pin files you want to track')).toBeInTheDocument()
  })

  it('falls back to the default pet name when none is supplied', () => {
    render(
      <PinnedSidePanel pins={[]} updatedPaths={new Set()} deletedPaths={new Set()} visible />,
    )
    expect(screen.getByText('Ask Mochi to pin files you want to track')).toBeInTheDocument()
  })

  it('groups pins under their parent folder and prefers an explicit label', () => {
    render(
      <PinnedSidePanel
        pins={[pin('/home/u/src/a.ts'), pin('/home/u/src/b.py'), pin('/home/u/docs/c.md', 'Notes')]}
        updatedPaths={new Set()} deletedPaths={new Set()} visible />,
    )
    expect(screen.getByText('src')).toBeInTheDocument()
    expect(screen.getByText('docs')).toBeInTheDocument()
    expect(screen.getByText('a.ts')).toBeInTheDocument()
    expect(screen.getByText('b.py')).toBeInTheDocument()
    // The label wins over the basename.
    expect(screen.getByText('Notes')).toBeInTheDocument()
    expect(screen.queryByText('c.md')).not.toBeInTheDocument()
  })

  it('previews a pin and marks it seen on click', async () => {
    const onMarkSeen = vi.fn()
    render(
      <PinnedSidePanel pins={[pin('/home/u/src/a.ts')]} updatedPaths={new Set(['/home/u/src/a.ts'])}
        deletedPaths={new Set()} visible onMarkSeen={onMarkSeen} />,
    )
    await userEvent.click(screen.getByText('a.ts'))
    expect(markPinnedSeen).toHaveBeenCalledWith('/home/u/src/a.ts')
    expect(previewFile).toHaveBeenCalledWith('/home/u/src/a.ts')
    expect(onMarkSeen).toHaveBeenCalledWith('/home/u/src/a.ts')
  })

  it('still marks a pin seen in a browser tab, but skips the shell-only preview', async () => {
    electronShell = false
    const onMarkSeen = vi.fn()
    render(
      <PinnedSidePanel pins={[pin('/home/u/src/a.ts')]} updatedPaths={new Set(['/home/u/src/a.ts'])}
        deletedPaths={new Set()} visible onMarkSeen={onMarkSeen} />,
    )
    await userEvent.click(screen.getByText('a.ts'))
    // Mark-seen is HTTP-backed and works everywhere; only the OS previewer
    // needs the shell, so that call alone is withheld.
    expect(markPinnedSeen).toHaveBeenCalledWith('/home/u/src/a.ts')
    expect(onMarkSeen).toHaveBeenCalledWith('/home/u/src/a.ts')
    expect(previewFile).not.toHaveBeenCalled()
  })

  it('renders a browser-tab pin with nothing to clear as inert, keeping unpin on hover', async () => {
    electronShell = false
    render(
      <PinnedSidePanel pins={[pin('/home/u/src/a.ts')]} updatedPaths={new Set()}
        deletedPaths={new Set()} visible />,
    )
    // No previewer and no update dot to clear: a click would have no visible
    // payoff, so the row must not present as a control at all.
    const label = screen.getByText('a.ts')
    expect(label.closest('[role="button"]')).toBeNull()
    fireEvent.click(label)
    expect(previewFile).not.toHaveBeenCalled()
    expect(markPinnedSeen).not.toHaveBeenCalled()
    // The unpin affordance is its own HTTP-backed control and stays reachable.
    await userEvent.hover(label)
    await userEvent.click(screen.getByRole('button', { name: 'Unpin' }))
    expect(unpinFile).toHaveBeenCalledWith('/home/u/src/a.ts')
  })

  it('reveals Unpin on hover and unpins on click', async () => {
    render(
      <PinnedSidePanel pins={[pin('/home/u/src/a.ts')]} updatedPaths={new Set()}
        deletedPaths={new Set()} visible />,
    )
    expect(screen.queryByRole('button', { name: 'Unpin' })).not.toBeInTheDocument()
    await userEvent.hover(screen.getByText('a.ts'))
    await userEvent.click(screen.getByRole('button', { name: 'Unpin' }))
    expect(unpinFile).toHaveBeenCalledWith('/home/u/src/a.ts')
  })

  it('offers no unpin affordance for a deleted pin', async () => {
    render(
      <PinnedSidePanel pins={[pin('/home/u/src/gone.ts')]} updatedPaths={new Set()}
        deletedPaths={new Set(['/home/u/src/gone.ts'])} visible />,
    )
    await userEvent.hover(screen.getByText('gone.ts'))
    expect(screen.queryByRole('button', { name: 'Unpin' })).not.toBeInTheDocument()
  })
})

describe('ChatPanel header', () => {
  it('shows the pet name with its state and mood', async () => {
    await renderPanel()
    expect(screen.getByText('Mochi')).toBeInTheDocument()
    expect(screen.getByText(/Idle/)).toHaveTextContent('Happy')
  })

  it('re-labels the state when the backend pushes a change', async () => {
    await renderPanel()
    emit('onStateChange', 'working')
    expect(await screen.findByText(/Working/)).toBeInTheDocument()
  })

  it('drops a neutral mood rather than labelling it', async () => {
    await renderPanel()
    emit('onMood', 'neutral')
    await waitFor(() => expect(screen.getByText(/Idle/)).not.toHaveTextContent('Happy'))
  })

  it('wires the pins and watchlist toggles to their callbacks', async () => {
    const onTogglePinned = vi.fn()
    const onToggleWatch = vi.fn()
    await renderPanel({ onTogglePinned, onToggleWatch, pinnedFileCount: 2 })
    await userEvent.click(screen.getByRole('button', { name: 'Pinned Files' }))
    await userEvent.click(screen.getByRole('button', { name: 'Watch List' }))
    expect(onTogglePinned).toHaveBeenCalledTimes(1)
    expect(onToggleWatch).toHaveBeenCalledTimes(1)
  })

  it('closes the chat window from the header', async () => {
    await renderPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(closeChat).toHaveBeenCalledTimes(1)
  })

  it('shows the context ring only once usage is known', async () => {
    await renderPanel()
    expect(screen.queryByTitle(/^Context:/)).not.toBeInTheDocument()
    emit('onContextUsage', 75)
    const ring = await screen.findByTitle('Context: 75%')
    expect(ring).toHaveTextContent('75')
  })
})

describe('ChatPanel history', () => {
  it('renders the loaded conversation, dropping non-chat roles', async () => {
    history = [
      { role: 'user', content: 'ping', timestamp: 1700000000000 },
      { role: 'assistant', content: 'pong', timestamp: 1700000001000 },
      { role: 'system', content: 'internal bookkeeping', timestamp: 1700000002000 },
    ]
    await renderPanel()
    expect(await screen.findByText('ping')).toBeInTheDocument()
    expect(screen.getByText('pong')).toBeInTheDocument()
    expect(screen.queryByText('internal bookkeeping')).not.toBeInTheDocument()
  })
})

describe('ChatPanel composer', () => {
  it('sends the typed text and clears the box', async () => {
    await renderPanel()
    await userEvent.type(composer(), 'hello pet')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith('hello pet', undefined))
    expect(composer()).toHaveValue('')
  })

  it('sends on Enter and keeps a Shift+Enter newline in the box', async () => {
    await renderPanel()
    await userEvent.type(composer(), 'first{Shift>}{Enter}{/Shift}second')
    expect(composer().value).toContain('\n')
    expect(sendMessage).not.toHaveBeenCalled()
    await userEvent.type(composer(), '{Enter}')
    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith('first\nsecond', undefined))
  })

  it('does nothing when the box is empty', async () => {
    await renderPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(sendMessage).not.toHaveBeenCalled()
  })

  it('restores the text and explains the failure when the send is refused', async () => {
    await renderPanel()
    sendMessage.mockRejectedValueOnce(new Error('offline'))
    await userEvent.type(composer(), 'keep me')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(
      await screen.findByText("Couldn't send — check your connection and try again."),
    ).toBeInTheDocument()
    // The typed text is not lost, so the user can retry.
    expect(composer()).toHaveValue('keep me')
  })

  it('dismisses the failure banner', async () => {
    await renderPanel()
    sendMessage.mockRejectedValueOnce(new Error('offline'))
    await userEvent.type(composer(), 'x')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await screen.findByText("Couldn't send — check your connection and try again.")
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() =>
      expect(
        screen.queryByText("Couldn't send — check your connection and try again."),
      ).not.toBeInTheDocument(),
    )
  })
})

describe('ChatPanel slash commands', () => {
  it('suggests matching commands with their descriptions', async () => {
    await renderPanel()
    await userEvent.type(composer(), '/c')
    expect(await screen.findByText('/clear')).toBeInTheDocument()
    expect(screen.getByText('Clear screen (history preserved)')).toBeInTheDocument()
    expect(screen.getByText('/compact')).toBeInTheDocument()
    expect(screen.getByText('/context')).toBeInTheDocument()
    // A non-matching command is filtered out.
    expect(screen.queryByText('/model')).not.toBeInTheDocument()
  })

  it('completes a command when its row is clicked', async () => {
    await renderPanel()
    await userEvent.type(composer(), '/co')
    await userEvent.click(screen.getByText('/compact'))
    expect(composer()).toHaveValue('/compact')
  })

  it('completes the highlighted command on Tab', async () => {
    await renderPanel()
    await userEvent.type(composer(), '/c{ArrowDown}{Tab}')
    // ArrowDown moves off /clear onto the second match.
    expect(composer()).toHaveValue('/compact')
  })

  it('wraps the highlight when arrowing up from the first row', async () => {
    await renderPanel()
    await userEvent.type(composer(), '/c{ArrowUp}{Enter}')
    expect(composer()).toHaveValue('/context')
  })

  it('hides the suggestions once the command is typed in full', async () => {
    await renderPanel()
    await userEvent.type(composer(), '/clear')
    expect(screen.queryByText('Clear screen (history preserved)')).not.toBeInTheDocument()
  })

  it('/clear empties the transcript without sending anything', async () => {
    history = [{ role: 'user', content: 'earlier turn', timestamp: 1700000000000 }]
    await renderPanel()
    await screen.findByText('earlier turn')
    await userEvent.type(composer(), '/clear')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(screen.queryByText('earlier turn')).not.toBeInTheDocument())
    expect(sendMessage).not.toHaveBeenCalled()
    // History is preserved, so it can be pulled back in.
    expect(screen.getByRole('button', { name: 'Load earlier messages' })).toBeInTheDocument()
  })

  it('restores cleared messages from the load-earlier button', async () => {
    history = [{ role: 'user', content: 'earlier turn', timestamp: 1700000000000 }]
    await renderPanel()
    await screen.findByText('earlier turn')
    await userEvent.type(composer(), '/clear')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(screen.queryByText('earlier turn')).not.toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: 'Load earlier messages' }))
    expect(await screen.findByText('earlier turn')).toBeInTheDocument()
  })

  it('/new starts a fresh session and reports both ends of it', async () => {
    await renderPanel()
    await userEvent.type(composer(), '/new')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByText('Starting fresh session…')).toBeInTheDocument()
    expect(await screen.findByText('New session started — context is fresh!')).toBeInTheDocument()
    expect(newSession).toHaveBeenCalledTimes(1)
    expect(sendMessage).not.toHaveBeenCalled()
  })

  it('/new surfaces a failure instead of claiming success', async () => {
    await renderPanel()
    newSession.mockRejectedValueOnce(new Error('no gateway'))
    await userEvent.type(composer(), '/new')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByText('Failed to start new session')).toBeInTheDocument()
    expect(screen.queryByText('New session started — context is fresh!')).not.toBeInTheDocument()
  })
})

describe('ChatPanel turn state', () => {
  it('raises the stop capsule for a turn and clears it on stop', async () => {
    await renderPanel()
    emit('onChatMessage', { id: 'm-1', role: 'user', content: 'do a thing', timestamp: 1700000000000 })
    expect(await screen.findByText('do a thing')).toBeInTheDocument()
    const stop = await screen.findByRole('button', { name: /Stop/ })
    await userEvent.click(stop)
    expect(stopGeneration).toHaveBeenCalledTimes(1)
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /Stop/ })).not.toBeInTheDocument(),
    )
  })

  it('ends the turn on a running -> idle slot transition', async () => {
    await renderPanel()
    emit('onChatMessage', { id: 'm-2', role: 'user', content: 'go', timestamp: 1700000000000 })
    await screen.findByRole('button', { name: /Stop/ })
    emit('onSlotsUpdate', [{ key: 'mochi', running: true }])
    emit('onSlotsUpdate', [{ key: 'mochi', running: false }])
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /Stop/ })).not.toBeInTheDocument(),
    )
  })

  it('does not re-count a backfilled message as a live turn', async () => {
    await renderPanel()
    emit('onChatMessage', {
      id: 'm-3', role: 'user', content: 'replayed', timestamp: 1700000000000, backfill: true,
    })
    expect(await screen.findByText('replayed')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Stop/ })).not.toBeInTheDocument()
  })
})

describe('ChatPanel offline banner', () => {
  it('offers to start the gateway and reports why it did not', async () => {
    backendOnline = false
    await renderPanel()
    expect(await findOfflineBanner()).toBeInTheDocument()
    retryConnect.mockResolvedValueOnce({ ok: false, message: 'Gateway refused' })
    await userEvent.click(screen.getByRole('button', { name: 'Start Kiro Crew' }))
    expect(await screen.findByText('Gateway refused')).toBeInTheDocument()
  })

  it('falls back to the timeout message when the retry throws', async () => {
    backendOnline = false
    await renderPanel()
    await findOfflineBanner()
    retryConnect.mockRejectedValueOnce(new Error('boom'))
    await userEvent.click(screen.getByRole('button', { name: 'Start Kiro Crew' }))
    expect(await screen.findByText(/Timed out/)).toBeInTheDocument()
  })

  it('hides the banner once the backend reports online', async () => {
    backendOnline = false
    await renderPanel()
    await findOfflineBanner()
    emit('onBackendStatus', true)
    await waitFor(() =>
      expect(screen.queryByText('Kiro Crew disconnected')).not.toBeInTheDocument(),
    )
  })
})

describe('ChatPanel context menu', () => {
  /** Right-click the panel shell to open its menu. */
  async function openMenu(container: HTMLElement) {
    fireEvent.contextMenu(container.firstChild as HTMLElement, { clientX: 5, clientY: 5 })
    return within(await screen.findByRole('menu'))
  }

  it('clears the screen from the menu', async () => {
    history = [{ role: 'user', content: 'visible turn', timestamp: 1700000000000 }]
    const { container } = await renderPanel()
    await screen.findByText('visible turn')
    const menu = await openMenu(container)
    await userEvent.click(menu.getByRole('menuitem', { name: 'Clear screen' }))
    await waitFor(() => expect(screen.queryByText('visible turn')).not.toBeInTheDocument())
  })

  it('opens settings, gallery, and the dashboard', async () => {
    const { container } = await renderPanel()
    let menu = await openMenu(container)
    await userEvent.click(menu.getByRole('menuitem', { name: 'Settings' }))
    menu = await openMenu(container)
    await userEvent.click(menu.getByRole('menuitem', { name: 'Appearance Gallery' }))
    menu = await openMenu(container)
    await userEvent.click(menu.getByRole('menuitem', { name: 'Kiro Crew Dashboard' }))
    expect(openSettings).toHaveBeenCalledTimes(1)
    expect(galleryOpen).toHaveBeenCalledTimes(1)
    expect(openDashboard).toHaveBeenCalledTimes(1)
  })

  it('confirms before deleting history, and can be cancelled', async () => {
    history = [{ role: 'user', content: 'doomed turn', timestamp: 1700000000000 }]
    const { container } = await renderPanel()
    await screen.findByText('doomed turn')

    let menu = await openMenu(container)
    await userEvent.click(menu.getByRole('menuitem', { name: 'Delete chat history' }))
    expect(await screen.findByText('Delete chat history?')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(deleteHistory).not.toHaveBeenCalled()
    expect(screen.getByText('doomed turn')).toBeInTheDocument()

    menu = await openMenu(container)
    await userEvent.click(menu.getByRole('menuitem', { name: 'Delete chat history' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(deleteHistory).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.queryByText('doomed turn')).not.toBeInTheDocument())
  })

  it('confirms before a full reset', async () => {
    history = [{ role: 'user', content: 'old turn', timestamp: 1700000000000 }]
    const { container } = await renderPanel()
    await screen.findByText('old turn')
    const menu = await openMenu(container)
    await userEvent.click(menu.getByRole('menuitem', { name: 'Reset Mochi' }))
    expect(await screen.findByText('Reset \u201cMochi\u201d?')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Reset' }))
    await waitFor(() => expect(resetMochi).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.queryByText('old turn')).not.toBeInTheDocument())
  })
})

describe('ChatPanel edit and resend', () => {
  it('loads the message back into the composer and resends it to the edit route', async () => {
    history = [{ role: 'user', content: 'typo here', timestamp: 1700000000000 }]
    await renderPanel()
    await screen.findByText('typo here')
    await userEvent.click(screen.getByRole('button', { name: 'Edit & resend' }))
    expect(composer()).toHaveValue('typo here')
    expect(screen.getByText('Editing — send to replace, or cancel')).toBeInTheDocument()

    await userEvent.clear(composer())
    await userEvent.type(composer(), 'fixed')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(editResend).toHaveBeenCalledWith('fixed', '1700000000000'))
    // The edited turn and everything after it is dropped locally.
    await waitFor(() => expect(screen.queryByText('typo here')).not.toBeInTheDocument())
  })

  it('falls back to a plain send when the edit route refuses', async () => {
    history = [{ role: 'user', content: 'original', timestamp: 1700000000000 }]
    await renderPanel()
    await screen.findByText('original')
    editResend.mockResolvedValueOnce({ ok: false })
    await userEvent.click(screen.getByRole('button', { name: 'Edit & resend' }))
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith('original', undefined))
  })

  it('cancels edit mode and empties the composer', async () => {
    history = [{ role: 'user', content: 'never mind', timestamp: 1700000000000 }]
    await renderPanel()
    await screen.findByText('never mind')
    await userEvent.click(screen.getByRole('button', { name: 'Edit & resend' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(composer()).toHaveValue('')
    expect(screen.queryByText('Editing — send to replace, or cancel')).not.toBeInTheDocument()
  })
})

describe('ChatPanel approval card', () => {
  it('asks about the tool and its input, with a trust hint when nothing is scopable', async () => {
    await renderPanel()
    emit('onApprovalRequest', approvalFrame())
    expect(await screen.findByText('execute_bash')).toBeInTheDocument()
    expect(screen.getByText('ls -la')).toBeInTheDocument()
    expect(screen.getByText(/Wants to run/)).toBeInTheDocument()
    expect(
      screen.getByText('Trust also auto-approves execute_bash from now on.'),
    ).toBeInTheDocument()
  })

  it('relabels the card once the approval reaches the agent', async () => {
    await renderPanel()
    emit('onApprovalRequest', approvalFrame())
    await userEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    expect(respondApproval).toHaveBeenCalledWith('req-1', 'approve', undefined)
    expect(await screen.findByText('Approved')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  })

  it('says Rejected, not Approved, when the user rejects', async () => {
    await renderPanel()
    emit('onApprovalRequest', approvalFrame())
    await userEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    expect(respondApproval).toHaveBeenCalledWith('req-1', 'reject', undefined)
    expect(await screen.findByText('Rejected')).toBeInTheDocument()
  })

  it('keeps the card and reports the error when the POST fails', async () => {
    await renderPanel()
    respondApproval.mockResolvedValueOnce({ ok: false })
    emit('onApprovalRequest', approvalFrame())
    await userEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    expect(
      await screen.findByText("Couldn't send — check your connection and try again."),
    ).toBeInTheDocument()
    // Claiming "Approved" here would fabricate a decision the agent never got.
    expect(screen.queryByText('Approved')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument()
  })

  it('reveals the scoped grants behind Trust instead of firing the widest one', async () => {
    await renderPanel()
    emit('onApprovalRequest', approvalFrame({ fullCommand: 'cat /etc/hosts', baseCommand: 'cat,wc' }))
    const trust = await screen.findByRole('button', { name: 'Trust' })
    expect(trust).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(trust)
    expect(respondApproval).not.toHaveBeenCalled()
    expect(trust).toHaveAttribute('aria-expanded', 'true')

    expect(screen.getByRole('button', { name: /cat \/etc\/hosts/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Trust all tools' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Trust all cat, wc commands' }))
    expect(respondApproval).toHaveBeenCalledWith('req-1', 'trust_base', 'cat *,wc *')
    expect(await screen.findByText('Trusted')).toBeInTheDocument()
  })

  it('grants only this command when the exact-command scope is picked', async () => {
    await renderPanel()
    emit('onApprovalRequest', approvalFrame({ fullCommand: 'cat /etc/hosts', baseCommand: 'cat' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Trust' }))
    await userEvent.click(screen.getByRole('button', { name: /cat \/etc\/hosts/ }))
    expect(respondApproval).toHaveBeenCalledWith('req-1', 'trust_command', 'cat /etc/hosts')
  })

  it('offers no family grant when it would duplicate the command grant', async () => {
    await renderPanel()
    emit('onApprovalRequest', approvalFrame({ fullCommand: 'fs_read', baseCommand: 'fs_read' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Trust' }))
    expect(screen.queryByRole('button', { name: /Trust all .* commands/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Trust all tools' })).toBeInTheDocument()
  })

  it('ignores a duplicate approval frame instead of stacking a second card', async () => {
    await renderPanel()
    emit('onApprovalRequest', approvalFrame())
    emit('onApprovalRequest', approvalFrame())
    await screen.findByText('execute_bash')
    expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(1)
  })

  it('carries the real verdict when the approval is resolved elsewhere', async () => {
    await renderPanel()
    emit('onApprovalRequest', approvalFrame())
    await screen.findByText('execute_bash')
    emit('onApprovalResolvedExternal', { id: 'req-1', approved: false })
    expect(await screen.findByText('Rejected')).toBeInTheDocument()
  })

  it('resolves every pending card when the frame names no request', async () => {
    await renderPanel()
    emit('onApprovalRequest', approvalFrame())
    emit('onApprovalRequest', approvalFrame({ id: 'req-2', tool: 'fs_write' }))
    await screen.findByText('fs_write')
    emit('onApprovalResolvedExternal', { approved: true })
    await waitFor(() => expect(screen.getAllByText('Approved')).toHaveLength(2))
  })
})

describe('ChatPanel bubbles', () => {
  it('treats a user-typed approval marker as ordinary text', async () => {
    // The marker is internal, but nothing stops a user typing it — parsing it
    // as a payload used to take the whole panel down to the error boundary.
    history = [{ role: 'user', content: '__approval__not-a-payload', timestamp: 1700000000000 }]
    await renderPanel()
    expect(await screen.findByText('__approval__not-a-payload')).toBeInTheDocument()
  })

  it('turns a trailing options list into buttons that send the choice', async () => {
    history = [
      {
        role: 'assistant',
        content: 'Pick one\n[OPTIONS: Merge it now | Show me the diff]',
        timestamp: 1700000000000,
      },
    ]
    await renderPanel()
    expect(await screen.findByText('Pick one')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Show me the diff' }))
    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith('Show me the diff', undefined))
  })

  it('draws nothing for a message whose only content is markup', async () => {
    history = [
      { role: 'assistant', content: '<br>', timestamp: 1700000000000 },
      { role: 'assistant', content: 'real answer', timestamp: 1700000001000 },
    ]
    await renderPanel()
    await screen.findByText('real answer')
    // One bubble, not two — an empty one would show a lone timestamp.
    expect(screen.getAllByRole('button', { name: 'Copy markdown' })).toHaveLength(1)
  })
})

describe('ChatPanel streaming footer', () => {
  /**
   * The chunk channel throttles through `requestAnimationFrame`, but the done
   * frame flushes whatever is still buffered synchronously — so driving both
   * gives the streaming bubble without waiting on a frame.
   */
  function stream(text: string) {
    emit('onChatChunk', text)
    emit('onChatDone')
  }

  it('renders the streamed answer as markdown', async () => {
    await renderPanel()
    stream('**bold** answer')
    const bold = await screen.findByText('bold')
    expect(bold.tagName).toBe('STRONG')
  })

  it('closes a code fence the stream has not finished yet', async () => {
    await renderPanel()
    stream('Here you go:\n```python\nprint(1)')
    // An unclosed fence would otherwise render as raw text with the backticks.
    expect(await screen.findByText('print(1)')).toBeInTheDocument()
    expect(screen.getByText('python')).toBeInTheDocument()
    expect(screen.queryByText(/```/)).not.toBeInTheDocument()
  })

  it('drops a half-arrived widget tag rather than showing its markup', async () => {
    await renderPanel()
    stream('Building it now <mcwidget title="Half')
    expect(await screen.findByText('Building it now')).toBeInTheDocument()
    // The stream commit is async; wait for React to flush before asserting the
    // negative, or a slow runner still sees the pre-strip markup and fails.
    await waitFor(() => expect(screen.queryByText(/mcwidget/)).not.toBeInTheDocument())
  })

  it('replaces the streamed text with the committed message', async () => {
    await renderPanel()
    stream('partial')
    await screen.findByText('partial')
    emit('onChatMessage', {
      id: 'a-1', role: 'assistant', content: 'final answer', timestamp: 1700000000000,
    })
    expect(await screen.findByText('final answer')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('partial')).not.toBeInTheDocument())
  })
})

describe('ChatPanel markdown affordances', () => {
  it('turns an inline file path into a chip that previews and reveals the file', async () => {
    history = [
      { role: 'assistant', content: 'Look at `src/main.py` first.', timestamp: 1700000000000 },
    ]
    await renderPanel()
    expect(await screen.findByTitle('src/main.py')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(previewFile).toHaveBeenCalledWith('src/main.py')
    await userEvent.click(screen.getByRole('button', { name: 'Show in file manager' }))
    expect(revealFile).toHaveBeenCalledWith('src/main.py')
  })

  it('renders the chip inert in a browser tab, where the shell bridge is absent', async () => {
    electronShell = false
    history = [
      { role: 'assistant', content: 'Look at `src/main.py` first.', timestamp: 1700000000000 },
    ]
    await renderPanel()
    // Preview and reveal both delegate to the shell bridge, so in a browser tab
    // the chip keeps the path (with its full-path tooltip) but offers no dead
    // controls: no buttons, and the label is plain text rather than focusable.
    const label = await screen.findByTitle('src/main.py')
    expect(label).not.toHaveAttribute('role')
    expect(screen.queryByRole('button', { name: 'Preview' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show in file manager' })).not.toBeInTheDocument()
    fireEvent.click(label)
    expect(previewFile).not.toHaveBeenCalled()
  })

  it('chips an absolute path found in ordinary prose', async () => {
    history = [
      { role: 'assistant', content: 'Wrote /home/u/notes.md for you.', timestamp: 1700000000000 },
    ]
    await renderPanel()
    const chip = await screen.findByTitle('/home/u/notes.md')
    // Long paths are shortened for display but keep the full path in the title.
    expect(chip).toHaveTextContent('u/notes.md')
    await userEvent.click(chip)
    expect(previewFile).toHaveBeenCalledWith('/home/u/notes.md')
  })

  it('opens a link in the OS browser instead of navigating the panel', async () => {
    history = [
      {
        role: 'assistant',
        content: 'See [the docs](https://example.com/guide).',
        timestamp: 1700000000000,
      },
    ]
    await renderPanel()
    await userEvent.click(await screen.findByText('the docs'))
    expect(openExternal).toHaveBeenCalledWith('https://example.com/guide')
  })

  it('reads local image bytes over the app api and opens the file on click', async () => {
    readLocalImage.mockResolvedValue('QUJD')
    history = [
      {
        role: 'assistant',
        content: 'Here it is ![shot](/home/u/shot.png)',
        timestamp: 1700000000000,
      },
    ]
    const { container } = await renderPanel()
    await waitFor(() => expect(readLocalImage).toHaveBeenCalledWith('/home/u/shot.png'))
    const img = await waitFor(() => {
      const found = container.querySelector('img[src^="data:image/png;base64,"]')
      expect(found).not.toBeNull()
      return found as HTMLImageElement
    })
    await userEvent.click(img)
    // The PATH, not the data URL — the OS viewer cannot open a data URL.
    expect(openLightbox).toHaveBeenCalledWith('/home/u/shot.png')
  })

  it('renders a bare image path the user typed as the image itself', async () => {
    readLocalImage.mockResolvedValue('QUJD')
    history = [{ role: 'user', content: '/home/u/photo.jpg', timestamp: 1700000000000 }]
    await renderPanel()
    await waitFor(() => expect(readLocalImage).toHaveBeenCalledWith('/home/u/photo.jpg'))
  })

  it('routes every absolute image path in a reply through the local reader', async () => {
    // The reply renderer lifts image references out of the markdown and hands
    // the PATH to LocalImage, because a bare `<img src="/…">` would be resolved
    // against the gateway origin and 404.
    history = [
      { role: 'assistant', content: '![logo](/home/u/logo.png)', timestamp: 1700000000000 },
    ]
    const { container } = await renderPanel()
    await waitFor(() => expect(readLocalImage).toHaveBeenCalledWith('/home/u/logo.png'))
    expect(container.querySelector('img[src="/home/u/logo.png"]')).toBeNull()
  })
})

describe('ChatPanel live config and presence', () => {
  it('renames the pet when the config is updated', async () => {
    await renderPanel()
    emit('onConfigUpdated', { petName: 'Kiro', theme: 'mocha' })
    expect(await screen.findByText('Kiro')).toBeInTheDocument()
  })

  it('survives a theme change pushed from settings', async () => {
    await renderPanel()
    emit('onThemeChanged', 'mocha')
    expect(screen.getByText('Mochi')).toBeInTheDocument()
  })

  it('labels a peek distinctly from plain idle', async () => {
    await renderPanel()
    emit('onPeeking', true)
    expect(await screen.findByText(/Peeking/)).toBeInTheDocument()
  })

  it('reports a backend switch as connecting rather than disconnected', async () => {
    backendOnline = false
    await renderPanel()
    emit('onBackendSwitching', true)
    expect(await screen.findByText('Connecting to Kiro Crew...', {}, { timeout: 5000 }))
      .toBeInTheDocument()
    expect(screen.queryByText('Kiro Crew disconnected')).not.toBeInTheDocument()
  })
})

describe('ChatPanel screenshot capture', () => {
  /** Answer the upload route without touching the network. */
  function stubUpload(response: { ok: boolean; body: unknown }) {
    return vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response(JSON.stringify(response.body), { status: response.ok ? 200 : 415 }),
    )
  }

  it('keeps a crop locally when the upload yields no attachment, and can remove it', async () => {
    const fetchSpy = stubUpload({ ok: true, body: { paths: [] } })
    try {
      const { container } = await renderPanel()
      // 'QUJD' is base64 for 'ABC' — cropToFile runs it through atob.
      emit('onCaptureDone', 'QUJD')
      const preview = await waitFor(() => {
        const found = container.querySelector('img[src="data:image/png;base64,QUJD"]')
        expect(found).not.toBeNull()
        return found as HTMLImageElement
      })
      await userEvent.click(screen.getByRole('button', { name: 'Remove screenshot' }))
      // The preview leaves on an exit animation, so it is still mounted until
      // that animation reports it finished.
      fireEvent.animationEnd(preview.parentElement!.parentElement!)
      await waitFor(() =>
        expect(container.querySelector('img[src="data:image/png;base64,QUJD"]')).toBeNull(),
      )
    } finally {
      fetchSpy.mockRestore()
    }
  })

  it('sends the pending crop alongside the text', async () => {
    const fetchSpy = stubUpload({ ok: true, body: { paths: [] } })
    try {
      const { container } = await renderPanel()
      emit('onCaptureDone', 'QUJD')
      await waitFor(() =>
        expect(container.querySelector('img[src="data:image/png;base64,QUJD"]')).not.toBeNull(),
      )
      await userEvent.type(composer(), 'what is this')
      await userEvent.click(screen.getByRole('button', { name: 'Send' }))
      await waitFor(() => expect(sendMessage).toHaveBeenCalledWith('what is this', 'QUJD'))
    } finally {
      fetchSpy.mockRestore()
    }
  })

  it('attaches an uploaded crop and references it in the sent message', async () => {
    const fetchSpy = stubUpload({ ok: true, body: { paths: ['/home/u/uploads/snip.png'] } })
    try {
      await renderPanel()
      emit('onCaptureDone', 'QUJD')
      // The strip is the record of what will be sent; the composer text stays clean.
      expect(await screen.findByAltText('snip.png')).toBeInTheDocument()
      await userEvent.type(composer(), 'crop it')
      expect(composer()).toHaveValue('crop it')
      await userEvent.click(screen.getByRole('button', { name: 'Send' }))
      // Referenced by path, so the crop reaches the agent as a real image
      // instead of living only in this window.
      await waitFor(() =>
        expect(sendMessage).toHaveBeenCalledWith(
          'crop it\n\n![image](/home/u/uploads/snip.png)',
          undefined,
        ),
      )
    } finally {
      fetchSpy.mockRestore()
    }
  })

  it('drops a queued attachment from the strip', async () => {
    const fetchSpy = stubUpload({ ok: true, body: { paths: ['/home/u/uploads/snip.png'] } })
    try {
      await renderPanel()
      emit('onCaptureDone', 'QUJD')
      await screen.findByAltText('snip.png')
      await userEvent.click(screen.getByRole('button', { name: 'Remove: snip.png' }))
      await waitFor(() => expect(screen.queryByAltText('snip.png')).not.toBeInTheDocument())
    } finally {
      fetchSpy.mockRestore()
    }
  })
})

describe('ChatPanel drop and paste', () => {
  /** A DataTransfer-shaped payload carrying one file. */
  function transferWith(file: File) {
    return {
      items: [{ kind: 'file', type: file.type, getAsFile: () => file }],
      files: [file],
      types: ['Files'],
    }
  }

  it('explains why a dropped file was refused instead of discarding it silently', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response(JSON.stringify({ error: 'Unsupported file type' }), { status: 415 }),
    )
    try {
      await renderPanel()
      const file = new File(['x'], 'thing.xyz', { type: 'application/octet-stream' })
      fireEvent.dragEnter(composer(), { dataTransfer: transferWith(file) })
      fireEvent.drop(composer(), { dataTransfer: transferWith(file) })
      expect(await screen.findByText('Unsupported file type')).toBeInTheDocument()
    } finally {
      fetchSpy.mockRestore()
    }
  })

  it('ignores an ordinary text paste', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    try {
      await renderPanel()
      fireEvent.paste(composer(), { clipboardData: { items: [], files: [], types: ['text/plain'] } })
      // No upload attempt: a text paste is just typing.
      expect(fetchSpy).not.toHaveBeenCalledWith('/api/upload/file', expect.anything())
    } finally {
      fetchSpy.mockRestore()
    }
  })
})

describe('ChatPanel copy to clipboard', () => {
  it('copies the reply markdown and confirms it on the button', async () => {
    const user = userEvent.setup()
    history = [{ role: 'assistant', content: 'the answer', timestamp: 1700000000000 }]
    await renderPanel()
    await user.click(await screen.findByRole('button', { name: 'Copy markdown' }))
    expect(await navigator.clipboard.readText()).toBe('the answer')
    // The button relabels itself so the copy is acknowledged.
    expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument()
  })
})

describe('ChatPanel drag highlight', () => {
  it('marks the composer as a drop target while a file is over it', async () => {
    await renderPanel()
    const box = composer()
    fireEvent.dragEnter(box, { dataTransfer: { items: [], files: [], types: ['Files'] } })
    fireEvent.dragOver(box, { dataTransfer: { items: [], files: [], types: ['Files'] } })
    expect(box.style.border).toContain('dashed')
    fireEvent.dragLeave(box)
    expect(box.style.border).not.toContain('dashed')
  })
})
