import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { createElement } from 'react'
import { cleanup, render, waitFor, act, fireEvent, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { APP_FRAME_TAG } from '../appFrame/frameProtocol'
import { claimableUrlHost as sharedUrlHost } from '../appFrame/hosts'
import { FRAME_SANDBOX } from '../appFrame/sandboxFrame'
import { fromFrame, installFrameWindows, postFrom, sentTo, type Sent } from '../test/LinkPreviewFramesTestUtils'
import { OPEN_DEADLINE_MS } from './LinkOpenLayer'
import { claimableUrlHost, forgetChoice, openerFor, rememberChoice, rememberedChoice, rememberedChoices, resolveLinkOpeners } from './linkOpenRegistry'

const listApps = vi.fn()
vi.mock('../api/client', () => ({ api: { listApps: () => listApps() } }))
const copyToClipboard = vi.fn(async (_text: string) => true)
vi.mock('../utils/clipboard', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/clipboard')>()),
  copyToClipboard: (text: string) => copyToClipboard(text),
}))

const APP = {
  name: 'tickets',
  displayName: 'Tickets',
  enabled: true,
  linkOpenGrant: { hosts: ['tickets.example.com', '.docs.example.com'], fullUrl: false },
  manifest: { contributes: { linkOpen: [{ id: 'open', entry: 'dist/open.mjs', hosts: ['tickets.example.com', '.docs.example.com'] }] } },
}

const mint = vi.fn(async (..._args: unknown[]) => ({ frame: '/app-frame/tickets/linkOpen/open/f/tok', frameId: 'f', capability: 'cap-1', ttl: 3600 }))
installFrameWindows()

/** The app as installed with whole URLs declared and approved. */
const FULL_APP = {
  ...APP,
  linkOpenGrant: { ...APP.linkOpenGrant, fullUrl: true },
  manifest: { contributes: { linkOpen: [{ ...APP.manifest.contributes.linkOpen[0], fullUrl: true }] } },
}
/** The same app after the owner withdrew the whole-URL grant. */
const WITHDRAWN_APP = { ...FULL_APP, linkOpenGrant: { ...FULL_APP.linkOpenGrant, fullUrl: false } }

let qc: QueryClient

/** Refetch the app list, which `listApps` now answers with `apps`. */
async function refetchApps(apps: unknown[]) {
  listApps.mockResolvedValue(apps)
  await act(async () => { await qc.invalidateQueries({ queryKey: ['apps'] }) })
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
}

async function mount() {
  const mod = await import('./LinkOpenLayer')
  mod.setLinkOpenMintForTest(mint)
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(createElement(QueryClientProvider, { client: qc }, createElement(mod.default)))
  await waitFor(() => expect(listApps).toHaveBeenCalled())
  await act(async () => { await vi.advanceTimersByTimeAsync(50) })
  return mod
}

/** A link in a chat message: inside the scope a MarkdownRenderer draws for chat. */
function link(href = 'https://tickets.example.com/t/TK-1?sig=secret#frag', scoped = true) {
  const a = document.createElement('a')
  a.href = href
  a.textContent = 'TK-1'
  const holder = document.createElement('div')
  if (scoped) holder.setAttribute('data-link-preview-scope', '')
  holder.append(a)
  document.body.append(holder)
  return a
}

/** Click `a` the way the user does; returns whether the host held the click. */
function click(a: HTMLElement, init: MouseEventInit = {}): boolean {
  const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ...init })
  a.dispatchEvent(ev)
  return ev.defaultPrevented
}

function theFrame(): HTMLIFrameElement {
  const el = document.querySelector<HTMLIFrameElement>('iframe[data-link-open-frame]')
  if (!el) throw new Error('no frame')
  return el
}

/** Choose "Open in app", let the frame load and say ready; returns the frame and what it was sent. */
async function openInApp() {
  fireEvent.click(screen.getByRole('button', { name: 'Open in Tickets' }))
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  const el = theFrame()
  fromFrame(el, 'ready')
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  return { el, sent: sentTo(el) }
}

const invokes = (sent: Sent[]) => sent.filter((m) => m.type === 'link.open')

let opened: string[] = []
beforeEach(() => {
  // happy-dom follows a click no handler held, which moves the page to that link's
  // origin; the layer treats links on the dashboard's own origin as the dashboard's.
  ;(window as unknown as { happyDOM: { setURL(url: string): void } }).happyDOM.setURL('http://localhost:6776/')
  vi.useFakeTimers({ shouldAdvanceTime: true })
  listApps.mockResolvedValue([APP])
  copyToClipboard.mockResolvedValue(true)
  opened = []
  vi.spyOn(window, 'open').mockImplementation((url?: string | URL) => { opened.push(String(url)); return null })
  window.localStorage.clear()
})
afterEach(async () => {
  const mod = await import('./LinkOpenLayer')
  mod.setLinkOpenMintForTest()
  listApps.mockReset()
  mint.mockClear()
  copyToClipboard.mockReset()
  // Unmount first: the dialogs are portalled into the body.
  cleanup()
  document.body.replaceChildren()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('linkOpen registry', () => {
  it('keeps only approved hosts and refuses public suffixes', () => {
    const [o] = resolveLinkOpeners([{ ...APP, linkOpenGrant: { hosts: ['tickets.example.com'] } }])
    expect([...o.exact]).toEqual(['tickets.example.com'])
    expect(o.suffixes).toEqual([])
    const refused = resolveLinkOpeners([{
      ...APP,
      linkOpenGrant: { hosts: ['co.uk', '.com', 'github.io'] },
      manifest: { contributes: { linkOpen: [{ id: 'open', entry: 'dist/open.mjs', hosts: ['co.uk', '.com', 'github.io'] }] } },
    }])
    expect(refused).toEqual([])
  })

  it('an app row without a grant, or a disabled app, gets no clicks', () => {
    expect(resolveLinkOpeners([{ ...APP, linkOpenGrant: undefined }])).toEqual([])
    expect(resolveLinkOpeners([{ ...APP, enabled: false }])).toEqual([])
  })

  it('matches https only, on the default port, without userinfo', () => {
    const openers = resolveLinkOpeners([APP])
    expect(openerFor('https://tickets.example.com/x', openers)?.host).toBe('tickets.example.com')
    expect(openerFor('https://a.docs.example.com/x', openers)?.host).toBe('a.docs.example.com')
    expect(openerFor('http://tickets.example.com/x', openers)).toBeNull()
    expect(openerFor('https://tickets.example.com:8443/x', openers)).toBeNull()
    expect(openerFor('https://u:p@tickets.example.com/x', openers)).toBeNull()
    expect(openerFor('https://other.example.com/x', openers)).toBeNull()
    expect(claimableUrlHost('https://TICKETS.example.com./x')).toBe('tickets.example.com')
  })

  it('refuses userinfo and explicit ports itself, where the shared rule takes them', () => {
    for (const href of [
      'https://u@tickets.example.com/x',
      'https://:p@tickets.example.com/x',
      'https://u:p@tickets.example.com/x',
      'https://tickets.example.com:8443/x',
      'https://tickets.example.com:1/x',
    ]) {
      expect(sharedUrlHost(href)).toBe('tickets.example.com')
      expect(claimableUrlHost(href)).toBeNull()
    }
    // `:443` is the default port, which URL drops: the same site, still claimed.
    expect(claimableUrlHost('https://tickets.example.com:443/x')).toBe('tickets.example.com')
  })

  it('a click on a claimed host with userinfo or a port is left alone', async () => {
    await mount()
    expect(click(link('https://u:p@tickets.example.com/t/TK-1'))).toBe(false)
    expect(click(link('https://tickets.example.com:8443/t/TK-1'))).toBe(false)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.queryByTestId('link-open-chooser')).toBeNull()
  })

  it('"Always" binds to one entry and one host, not every host the app claims', () => {
    rememberChoice('tickets:open', 'tickets.example.com', 'app')
    expect(rememberedChoice('tickets:open', 'tickets.example.com')).toBe('app')
    expect(rememberedChoice('tickets:open', 'a.docs.example.com')).toBeNull()
    expect(rememberedChoice('other:open', 'tickets.example.com')).toBeNull()
    expect(rememberedChoices('tickets')).toEqual([{ key: 'tickets:open', host: 'tickets.example.com', choice: 'app' }])
    forgetChoice('tickets:open', 'tickets.example.com')
    expect(rememberedChoice('tickets:open', 'tickets.example.com')).toBeNull()
  })
})

describe('LinkOpenLayer: the host owns the click', () => {
  it('leaves every click it does not own alone', async () => {
    await mount()
    expect(click(link('https://elsewhere.example.org/x'))).toBe(false)
    expect(click(link(undefined, false))).toBe(false)
    const a = link()
    expect(click(a, { metaKey: true })).toBe(false)
    expect(click(a, { ctrlKey: true })).toBe(false)
    expect(click(a, { shiftKey: true })).toBe(false)
    expect(click(a, { button: 1 })).toBe(false)
    expect(screen.queryByTestId('link-open-chooser')).toBeNull()
    expect(mint).not.toHaveBeenCalled()
  })

  it("the dashboard's own links stay with the dashboard, even on a host the app claims", async () => {
    const { heldLinkOf } = await import('./LinkOpenLayer')
    const openers = resolveLinkOpeners([APP])
    const heldAt = (href: string, origin: string) => {
      let held: unknown = 'unset'
      const a = link(href)
      const read = (e: MouseEvent) => { held = heldLinkOf(e, openers, origin) }
      document.addEventListener('click', read, true)
      a.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }))
      document.removeEventListener('click', read, true)
      return held
    }
    // Served at a host the grant covers: artifact and session links are not held.
    expect(heldAt('https://a.docs.example.com/artifacts/report', 'https://a.docs.example.com')).toBeNull()
    expect(heldAt('https://a.docs.example.com/chat?sid=s1', 'https://a.docs.example.com')).toBeNull()
    // The same host seen from another dashboard origin is still the app's.
    expect(heldAt('https://a.docs.example.com/artifacts/report', 'https://crew.example.org')).toMatchObject({ host: 'a.docs.example.com' })
  })

  it('a plain click on a claimed link asks first, and no app code runs until the user picks the app', async () => {
    await mount()
    expect(click(link())).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('link-open-chooser')).toBeTruthy()
    // What the app would get: no query, no fragment.
    expect(screen.getByText('https://tickets.example.com/t/TK-1')).toBeTruthy()
    expect(mint).not.toHaveBeenCalled()
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('Open in browser opens inside the click, and "Always" makes the next click an ordinary link', async () => {
    await mount()
    const a = link()
    click(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Open in browser' }))
    // Synchronous, in the button's own click: not an async open a blocker refuses.
    expect(opened).toEqual([a.href])
    expect(rememberedChoice('tickets:open', 'tickets.example.com')).toBe('browser')
    expect(click(a)).toBe(false)
    expect(mint).not.toHaveBeenCalled()
  })
})

describe('LinkOpenLayer: one invocation per open', () => {
  it('the frame is sandboxed and gets only the stripped URL, with an id and a deadline', async () => {
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const { el, sent } = await openInApp()
    expect(el.getAttribute('sandbox')).toBe(FRAME_SANDBOX)
    expect(el.getAttribute('sandbox')).not.toMatch(/allow-same-origin/)
    expect(el.getAttribute('src')).toBe('/app-frame/tickets/linkOpen/open/f/tok')
    expect(mint.mock.calls[0].slice(0, 3)).toEqual(['tickets', 'linkOpen', 'open'])
    // The gateway checks the clicked host and the URL mode against the live grant.
    expect(mint.mock.calls[0][4]).toEqual({ host: 'tickets.example.com', url: 'stripped' })
    expect(sent[0]).toMatchObject({ [APP_FRAME_TAG]: 1, type: 'init', capability: 'cap-1' })
    const [inv] = invokes(sent)
    expect(inv).toMatchObject({ deadlineMs: OPEN_DEADLINE_MS, url: 'https://tickets.example.com/t/TK-1' })
    expect(String(inv.invocationId)).toMatch(/^[0-9a-f-]{36}$/)
    expect(screen.getByTestId('link-open-panel').dataset.state).toBe('opening')
    fromFrame(el, 'invocation.resize', { invocationId: inv.invocationId, width: 600, height: 400 })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('link-open-panel').dataset.state).toBe('open')
    expect(el.style.visibility).toBe('visible')
    // Shown: the deadline no longer runs while the user reads.
    await act(async () => { await vi.advanceTimersByTimeAsync(OPEN_DEADLINE_MS * 3) })
    expect(screen.getByTestId('link-open-panel').dataset.state).toBe('open')
    expect(sent.some((m) => m.type === 'invoke.cancel')).toBe(false)
  })

  it('an answer for another invocation, or from another window, is dropped', async () => {
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const { el } = await openInApp()
    fromFrame(el, 'invocation.resize', { invocationId: 'ffffffff-ffff-4fff-8fff-ffffffffffff', width: 1, height: 1 })
    postFrom({ [APP_FRAME_TAG]: 1, type: 'invocation.resize', invocationId: invokes(sentTo(el))[0].invocationId, width: 1, height: 1 }, window)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('link-open-panel').dataset.state).toBe('opening')
  })

  it('past the deadline the host cancels and offers the browser', async () => {
    await mount()
    const a = link()
    click(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const { el, sent } = await openInApp()
    const id = invokes(sent)[0].invocationId
    await act(async () => { await vi.advanceTimersByTimeAsync(OPEN_DEADLINE_MS + 10) })
    expect(sent.find((m) => m.type === 'invoke.cancel')).toMatchObject({ invocationId: id, reason: 'deadline' })
    expect(screen.getByTestId('link-open-failed')).toBeTruthy()
    // A late answer does nothing.
    fromFrame(el, 'invocation.resize', { invocationId: id, width: 1, height: 1 })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('link-open-panel').dataset.state).toBe('failed')
    fireEvent.click(screen.getAllByRole('button', { name: 'Open in browser' }).at(-1)!)
    expect(opened).toEqual([a.href])
  })

  it('an error answer fails the open', async () => {
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const { el, sent } = await openInApp()
    fromFrame(el, 'invocation.error', { invocationId: invokes(sent)[0].invocationId, message: 'nope' })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('link-open-panel').dataset.state).toBe('failed')
  })

  it('closing the dialog cancels the open invocation and removes the frame', async () => {
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const { el, sent } = await openInApp()
    const id = invokes(sent)[0].invocationId
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    expect(sent.find((m) => m.type === 'invoke.cancel')).toMatchObject({ invocationId: id, reason: 'closed' })
    expect(sent.some((m) => m.type === 'dispose')).toBe(true)
    expect(el.isConnected).toBe(false)
  })

  it('"Always" for the app opens the next click on that host straight in the app, and no other host', async () => {
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    fireEvent.click(screen.getByRole('checkbox'))
    const { el, sent } = await openInApp()
    fromFrame(el, 'invocation.close', { invocationId: invokes(sent)[0].invocationId })
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    expect(document.querySelector('iframe[data-link-open-frame]')).toBeNull()
    expect(click(link('https://tickets.example.com/t/TK-2'))).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.queryByTestId('link-open-chooser')).toBeNull()
    expect(document.querySelector('iframe[data-link-open-frame]')).not.toBeNull()
    expect(mint).toHaveBeenCalledTimes(2)
    // Another host the same app claims still asks.
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    click(link('https://a.docs.example.com/page'))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('link-open-chooser')).toBeTruthy()
  })
  it('an offer from the app is only a host button, acted on by the click', async () => {
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const { el, sent } = await openInApp()
    const id = invokes(sent)[0].invocationId
    fromFrame(el, 'invocation.resize', { invocationId: id, width: 600, height: 400 })
    fromFrame(el, 'result.offer', { invocationId: id, seq: 1, kind: 'open', value: 'https://tickets.example.com/t/TK-1/edit' })
    fromFrame(el, 'result.offer', { invocationId: id, seq: 2, kind: 'open', value: 'javascript:alert(1)' })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(sent.filter((m) => m.type === 'result.ack').map((m) => m.seq)).toEqual([1])
    expect(sent.find((m) => m.type === 'result.error')).toMatchObject({ seq: 2, code: 'not_allowed' })
    expect(opened).toEqual([])
    // Offers sit in the overflow menu, beside the host's own open.
    expect(screen.queryByTestId('link-open-offer-open')).toBeNull()
    fireEvent.keyDown(screen.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('link-open-offer-open'))
    expect(opened).toEqual(['https://tickets.example.com/t/TK-1/edit'])
  })

  it('the header holds one menu beside close, and Open in browser is in it', async () => {
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const { el, sent } = await openInApp()
    fromFrame(el, 'invocation.resize', { invocationId: invokes(sent)[0].invocationId, width: 600, height: 400 })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    // With no offer the menu is still there, so the host's own open is always one click away.
    expect(screen.queryByTestId('link-open-in-browser')).toBeNull()
    fireEvent.keyDown(screen.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('link-open-in-browser'))
    expect(opened).toEqual(['https://tickets.example.com/t/TK-1?sig=secret#frag'])
    expect(screen.queryByTestId('link-open-offer-copy')).toBeNull()
  })

  it('a copy that does not reach the clipboard says so in the dialog', async () => {
    copyToClipboard.mockResolvedValue(false)
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const { el, sent } = await openInApp()
    const id = invokes(sent)[0].invocationId
    fromFrame(el, 'invocation.resize', { invocationId: id, width: 600, height: 400 })
    fromFrame(el, 'result.offer', { invocationId: id, seq: 1, kind: 'copy', value: 'TK-1' })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.queryByTestId('link-open-copy-failed')).toBeNull()
    fireEvent.keyDown(screen.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('link-open-offer-copy'))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(copyToClipboard).toHaveBeenCalledWith('TK-1')
    expect(screen.getByTestId('link-open-copy-failed').textContent).toContain("Couldn't copy.")
  })
})

describe('LinkOpenLayer: a held link follows the current grant', () => {
  it('the question picks up a withdrawn whole-URL grant before the app is handed the link', async () => {
    listApps.mockResolvedValue([FULL_APP])
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('https://tickets.example.com/t/TK-1?sig=secret#frag')).toBeTruthy()
    await refetchApps([WITHDRAWN_APP])
    expect(screen.getByText('https://tickets.example.com/t/TK-1')).toBeTruthy()
    const { sent } = await openInApp()
    expect(invokes(sent)[0]).toMatchObject({ url: 'https://tickets.example.com/t/TK-1' })
  })

  it('an open dialog closes when its whole-URL grant is withdrawn', async () => {
    listApps.mockResolvedValue([FULL_APP])
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const { el, sent } = await openInApp()
    expect(invokes(sent)[0]).toMatchObject({ url: 'https://tickets.example.com/t/TK-1?sig=secret#frag' })
    // The mint asked for a whole URL, so a gateway whose grant was withdrawn refuses it.
    expect(mint.mock.calls[0][4]).toEqual({ host: 'tickets.example.com', url: 'full' })
    fromFrame(el, 'invocation.resize', { invocationId: invokes(sent)[0].invocationId, width: 600, height: 400 })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await refetchApps([WITHDRAWN_APP])
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    expect(screen.queryByTestId('link-open-panel')).toBeNull()
    expect(el.isConnected).toBe(false)
  })

  it('an ordinary refetch keeps the open dialog and its frame', async () => {
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const { el } = await openInApp()
    // A changed list (another app installed) with this app's grant unchanged.
    await refetchApps([APP, { name: 'notes', enabled: true, manifest: {} }])
    expect(el.isConnected).toBe(true)
    expect(mint).toHaveBeenCalledTimes(1)
  })

  it('a question whose host the app no longer claims goes away', async () => {
    await mount()
    click(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByTestId('link-open-chooser')).toBeTruthy()
    await refetchApps([{ ...APP, linkOpenGrant: { ...APP.linkOpenGrant, hosts: ['.docs.example.com'] } }])
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    expect(screen.queryByTestId('link-open-chooser')).toBeNull()
  })
})
