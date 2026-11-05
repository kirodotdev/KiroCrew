/**
 * Message actions: what an app sees on a click, and what it never sees.
 */
import type React from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { APP_FRAME_TAG } from '../appFrame/frameProtocol'
import { FRAME_SANDBOX } from '../appFrame/sandboxFrame'
import { installFrameWindows, postFrom, sentTo, type Sent } from '../test/LinkPreviewFramesTestUtils'
import { INVOCATION_DEADLINE_MS, MessageActionPopover, READY_TIMEOUT_MS, setMessageActionMintForTest, type CancelReason } from './MessageActionLayer'
import { messageActionGrantLabel, resolveMessageActions, type ResolvedMessageAction } from './messageActionRegistry'
import { messageTextFor, readMessageText, truncateUnits } from './messageText'
import { holdVisibleCodeText } from '../utils/visibleText'
import { CodeBlock } from '../components/CodeBlock'
import MarkdownRenderer from '../components/MarkdownRenderer'

const copyToClipboard = vi.fn(async (_text: string) => true)
vi.mock('../utils/clipboard', () => ({ copyToClipboard: (t: string) => copyToClipboard(t) }))
const openExternal = vi.fn()
vi.mock('../hooks/useFrameOpenLink', async (orig) => ({ ...(await orig<object>()), openExternal: (u: string) => openExternal(u) }))

const app = (over: Record<string, unknown> = {}) => ({
  name: 'notes-app',
  displayName: 'Notes',
  enabled: true,
  version: '1.0.0',
  manifest: { contributes: { messageActions: [{ id: 'save', title: 'Save to notes', entry: 'dist/frame.mjs' }] } },
  messageActionGrant: {
    actions: [{ id: 'save', title: 'Save to notes', roles: ['assistant'], format: 'plain', maxChars: 32000, popover: { width: 360, maxHeight: 480 } }],
    staged: [],
  },
  ...over,
})

describe('resolveMessageActions', () => {
  it('offers only approved, still-declared actions of enabled apps', () => {
    expect(resolveMessageActions([app()]).map((a) => a.key)).toEqual(['notes-app:save'])
    expect(resolveMessageActions([app({ enabled: false })])).toEqual([])
    // Declared but not approved: the grant row is the authority.
    expect(resolveMessageActions([app({ messageActionGrant: { actions: [] } })])).toEqual([])
    expect(resolveMessageActions([app({ messageActionGrant: undefined })])).toEqual([])
    // Approved but no longer declared (or declared with a bad entry).
    expect(resolveMessageActions([app({ manifest: { contributes: { messageActions: [] } } })])).toEqual([])
    expect(resolveMessageActions([app({ manifest: { contributes: { messageActions: [{ id: 'save', entry: '../x.mjs' }] } } })])).toEqual([])
  })

  it('caps actions per app, clamps bounds and orders by app name', () => {
    const rows = ['a', 'b', 'c'].map((id) => ({ id, title: id.toUpperCase(), roles: ['assistant', 'user', 'bogus'], format: 'markdown', maxChars: 99999, popover: { width: 9000, maxHeight: 1 } }))
    const many = app({
      name: 'zeta', displayName: 'Zeta',
      manifest: { contributes: { messageActions: rows.map((r) => ({ id: r.id, entry: 'f.mjs' })) } },
      messageActionGrant: { actions: rows },
    })
    const out = resolveMessageActions([many, app()])
    expect(out.map((a) => a.key)).toEqual(['notes-app:save', 'zeta:a', 'zeta:b'])
    const z = out[1]
    expect(z.roles).toEqual(['assistant', 'user'])
    expect([z.format, z.maxChars, z.width, z.maxHeight]).toEqual(['markdown', 32000, 480, 120])
  })

  it('names a held-back entry in plain words', () => {
    const declared = [{ id: 'save', title: 'Save to notes' }]
    expect(messageActionGrantLabel('save:role:user', declared)).toContain('Save to notes')
    expect(messageActionGrantLabel('save:format:markdown', declared)).toContain('markdown')
    expect(messageActionGrantLabel('weird', declared)).toBe('weird')
  })
})

describe('message text: the visible text of the bubble', () => {
  // A bubble whose content root holds `content`, as the renderers mark it.
  const bubble = (content: React.ReactNode, around: React.ReactNode = null) => {
    const view = render(<div data-testid="bubble">{around}<div data-message-body="">{content}</div></div>)
    return { el: screen.getByTestId('bubble'), view }
  }
  const textOf = (content: React.ReactNode, around: React.ReactNode = null) => {
    const { el, view } = bubble(content, around)
    const text = readMessageText(el)
    view.unmount()
    return text
  }
  const katex = (tex: string, glyphs: string, extra = '') => (
    <span className={`katex${extra}`}>
      <span className="katex-mathml"><math><semantics><mrow><mi>x</mi></mrow><annotation encoding="application/x-tex">{tex}</annotation></semantics></math></span>
      <span className="katex-html" aria-hidden="true"><span>{glyphs}</span></span>
    </span>
  )

  it('gives a link its label, an image nothing, and skips what the reader cannot see', () => {
    expect(textOf(<>
      <h1>Title</h1>
      <p>See <a href="https://secret.example.com/?token=abc">the doc</a> and <strong>bold</strong>.<img src="https://images.example/p.png" alt="chart" /></p>
      <ul><li>one</li><li>two</li></ul>
      <span className="sr-only">screen reader only</span><span aria-hidden="true">decor</span><span hidden>gone</span>
      <span style={{ display: 'none' }}>none</span><span style={{ visibility: 'hidden' }}>invisible</span><button type="button">Copy</button>
    </>)).toBe('Title\nSee the doc and bold.\none\ntwo')
  })

  it('reads a KaTeX formula once, as its TeX, and never a hidden one', () => {
    expect(textOf(<p>Area: {katex('\\pi r^2', 'πr2')} units.</p>)).toBe('Area: \\pi r^2 units.')
    expect(textOf(<p>Visible <span className="sr-only">{katex('hidden_formula', 'h')}</span></p>)).toBe('Visible')
    expect(textOf(<p>Shown <span aria-hidden="true">{katex('hidden_two', 'h')}</span></p>)).toBe('Shown')
    // The formula root itself hidden, the class the sanitizer keeps on it.
    expect(textOf(<p>Visible {katex('hidden_root', 'h', ' sr-only')}</p>)).toBe('Visible')
  })

  it('gives a rendered diagram nothing: no source and no SVG labels', () => {
    expect(textOf(<>
      <p>Flow:</p>
      <div className="mermaid-block"><svg><g><text>Start</text><text>End</text></g></svg></div>
      <canvas aria-label="chart" />
      <p>Done.</p>
    </>)).toBe('Flow:\nDone.')
  })

  it('gives a registered code block exactly its code: no language label, line numbers or button text', () => {
    const { el, view } = bubble(<>
      <p>Run:</p>
      <div className="code-block" data-testid="code"><div><span>python</span><button type="button">Wrap</button><button type="button">Copy</button></div>
        <div><span>1</span><span>2</span>x = 1 y = 2</div></div>
    </>)
    const release = holdVisibleCodeText(screen.getByTestId('code'), 'x = 1\ny = 2')
    expect(readMessageText(el)).toBe('Run:\nx = 1\ny = 2')
    release()
    view.unmount()
  })

  it('reads the real renderer: a link as its label, and the Raw view as the source it shows', () => {
    const rendered = render(<div data-testid="rendered"><MarkdownRenderer content="See [the doc](https://secret.example.com/x) now." /></div>)
    expect(readMessageText(screen.getByTestId('rendered'))).toBe('See the doc now.')
    rendered.unmount()
    const raw = render(<div data-testid="raw"><MarkdownRenderer content="See [the doc](https://secret.example.com/x) now." rawMode /></div>)
    expect(readMessageText(screen.getByTestId('raw'))).toBe('See [the doc](https://secret.example.com/x) now.')
    raw.unmount()
  })

  it('reads a real CodeBlock as its code', () => {
    expect(textOf(<CodeBlock code={'print("hi")\nprint("bye")'} lang="python" complete={false} />)).toBe('print("hi")\nprint("bye")')
  })

  it('keeps preformatted whitespace, breaks and table cells', () => {
    const text = textOf(<>
      <pre>{'  a\n    b'}</pre>
      <p>line one<br />line two</p>
      <table><tbody><tr><th>h1</th><th>h2</th></tr><tr><td>c1</td><td>c2</td></tr></tbody></table>
    </>)!
    expect(text).toContain('  a\n    b')
    expect(text).toContain('line one\nline two')
    expect(text).toMatch(/\nh1\th2\n/)
    expect(text).toMatch(/\nc1\tc2$/)
  })

  it('keeps the spacing of white-space: pre-wrap text, as a plain user message shows it', () => {
    expect(textOf(<span style={{ whiteSpace: 'pre-wrap' }}>{'line  one\n    indented'}</span>)).toBe('line  one\n    indented')
  })

  it('reads a closed details as its summary only, and clipped text in full', () => {
    expect(textOf(<>
      <details><summary>More</summary><p>hidden body</p></details>
      <div style={{ maxHeight: 10, overflow: 'hidden' }}>clipped but present</div>
    </>)).toBe('More\nclipped but present')
    expect(textOf(<details open><summary>More</summary><p>shown body</p></details>)).toBe('More\nshown body')
  })

  it('passes every character through unchanged, private-use ones included', () => {
    expect(textOf(<><p>{'icon \uE000 here'}</p><pre>{'a\uE001\tb\uE002'}</pre></>)).toBe('icon \uE000 here\na\uE001\tb\uE002')
  })

  it('reads only the content roots, never what sits around them in the bubble', () => {
    const view = render(
      <div data-testid="bubble">
        <p>Session seeded from X</p>
        <blockquote>quoted other message</blockquote>
        <div>Knowledge: secret doc text</div>
        <div data-message-body=""><p>Mine.</p></div>
        <div><span>report.pdf</span></div>
        <div data-message-body=""><span>second segment</span></div>
      </div>,
    )
    expect(readMessageText(screen.getByTestId('bubble'))).toBe('Mine.\nsecond segment')
    view.unmount()
    expect(textOf(<p>outer <span data-message-body="">inner</span></p>)).toBe('outer inner')
  })

  it('refuses a bubble that is not on the page, has no content root, or shows nothing', () => {
    expect(readMessageText(null)).toBeNull()
    const detached = document.createElement('div')
    const root = document.createElement('div')
    root.setAttribute('data-message-body', '')
    root.textContent = 'text'
    detached.append(root)
    expect(readMessageText(detached)).toBeNull()
    expect(textOf(<svg />)).toBeNull()
    const view = render(<div data-testid="host-drawn"><p>host drawn</p></div>)
    expect(readMessageText(screen.getByTestId('host-drawn'))).toBeNull()
    view.unmount()
  })

  it('hands markdown over only in the markdown format, refuses unreadable plain, and caps either', () => {
    const source = { markdown: 'A [link](https://x.example/)', text: 'A link' }
    expect(messageTextFor(source, 'markdown', 100)).toEqual({ text: source.markdown, truncated: false })
    expect(messageTextFor(source, 'plain', 100)).toEqual({ text: 'A link', truncated: false })
    expect(messageTextFor(source, 'plain', 3)).toEqual({ text: 'A l', truncated: true })
    expect(messageTextFor({ ...source, text: null }, 'plain', 100)).toBeNull()
    expect(messageTextFor({ ...source, text: null }, 'markdown', 100)).toEqual({ text: source.markdown, truncated: false })
  })

  it('never cuts a surrogate pair', () => {
    expect(truncateUnits('ab\u{1F4DD}c', 3)).toEqual({ text: 'ab', truncated: true })
    expect(truncateUnits('abc', 3)).toEqual({ text: 'abc', truncated: false })
  })
})

describe('MessageActionPopover', () => {
  beforeAll(() => installFrameWindows())
  const action: ResolvedMessageAction = resolveMessageActions([app()])[0]
  const mint = vi.fn(async () => ({ frame: '/app-frame/notes-app/messageActions/save/fid/tok', frameId: 'fid', capability: 'cap-1', ttl: 900 }))
  beforeEach(() => {
    setMessageActionMintForTest(mint)
    copyToClipboard.mockClear()
    openExternal.mockClear()
    mint.mockClear()
  })
  afterEach(() => {
    setMessageActionMintForTest()
    vi.useRealTimers()
  })

  async function openFrame(onClose: (r?: CancelReason) => void = vi.fn(), markdown = 'Hello [docs](https://secret.example.com/x)', text: string | null = 'Hello docs') {
    const anchor = document.createElement('div')
    anchor.textContent = 'bubble'
    document.body.appendChild(anchor)
    const other = document.createElement('div')
    other.textContent = 'Other message with a secret'
    document.body.appendChild(other)
    const reason: { current: CancelReason } = { current: 'closed' }
    const view = render(<MessageActionPopover req={{ action, source: { markdown, text, role: 'assistant' }, anchor }} reasonRef={reason} onClose={onClose} />)
    const el = await waitFor(() => {
      const f = document.querySelector<HTMLIFrameElement>('iframe[data-message-action-frame]')
      if (!f) throw new Error('no frame yet')
      return f
    })
    await waitFor(() => expect(sentTo(el)[0]).toMatchObject({ type: 'init' }))
    return { el, view, onClose }
  }
  const from = (el: HTMLIFrameElement, type: string, data: Sent = {}) =>
    act(() => postFrom({ [APP_FRAME_TAG]: 1, type, ...data }, el.contentWindow))
  const invokeOf = (el: HTMLIFrameElement) => sentTo(el).find((m) => m.type === 'message.invoke') as Sent

  it('refuses cleanly when the bubble could not be read: no frame, nothing sent', () => {
    const anchor = document.createElement('div')
    document.body.appendChild(anchor)
    const reason: { current: CancelReason } = { current: 'closed' }
    render(<MessageActionPopover req={{ action, source: { markdown: 'Hi', text: null, role: 'assistant' }, anchor }} reasonRef={reason} onClose={vi.fn()} />)
    expect(screen.getByRole('alert').textContent).toContain("Couldn't read this message")
    expect(document.querySelector('iframe[data-message-action-frame]')).toBeNull()
    expect(mint).not.toHaveBeenCalled()
  })

  it('mints for this click: the role and the approved format go to the gateway to check', async () => {
    await openFrame()
    expect(mint).toHaveBeenCalledWith(action, 'assistant')
  })

  it('shows the failure and sends nothing when the gateway refuses the mint', async () => {
    mint.mockRejectedValueOnce(new Error('app frame mint failed: 403'))
    const anchor = document.createElement('div')
    document.body.appendChild(anchor)
    const reason: { current: CancelReason } = { current: 'closed' }
    render(<MessageActionPopover req={{ action, source: { markdown: 'Hi', text: 'Hi', role: 'user' }, anchor }} reasonRef={reason} onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByTestId('message-action-failed')).toBeTruthy())
    const frame = document.querySelector<HTMLIFrameElement>('iframe[data-message-action-frame]')
    expect(frame ? sentTo(frame).some((m) => m.type === 'message.invoke') : false).toBe(false)
  })

  it('loads the shared sandbox and hands over this message only, as rendered text', async () => {
    const { el } = await openFrame()
    expect(el.getAttribute('sandbox')).toBe(FRAME_SANDBOX)
    expect(el.getAttribute('src')).toBe('/app-frame/notes-app/messageActions/save/fid/tok')
    expect(sentTo(el)[0]).toMatchObject({ type: 'init', capability: 'cap-1' })
    from(el, 'ready')
    const inv = invokeOf(el)
    expect(inv).toMatchObject({ text: 'Hello docs', format: 'plain', truncated: false, role: 'assistant', deadlineMs: INVOCATION_DEADLINE_MS })
    expect(typeof inv.invocationId).toBe('string')
    const everything = JSON.stringify(sentTo(el))
    expect(everything).not.toContain('secret')
    expect(everything).not.toContain('Other message')
    expect(screen.getByTestId('message-action-attribution').textContent).toContain('Notes')
  })

  it('acks a current offer, refuses a stale or out-of-order one, and copies only on click', async () => {
    const { el } = await openFrame()
    from(el, 'ready')
    const id = invokeOf(el).invocationId
    from(el, 'result.offer', { invocationId: id, seq: 1, kind: 'copy', value: 'note link' })
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ type: 'result.ack', invocationId: id, seq: 1 }))
    expect(copyToClipboard).not.toHaveBeenCalled()
    from(el, 'result.offer', { invocationId: id, seq: 1, kind: 'copy', value: 'again' })
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ type: 'result.error', code: 'stale' }))
    from(el, 'result.offer', { invocationId: 'forged', seq: 2, kind: 'copy', value: 'x' })
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ type: 'result.error', code: 'stale' }))
    from(el, 'result.offer', { invocationId: id, seq: 3, kind: 'open', value: 'javascript:alert(1)' })
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ type: 'result.error', code: 'not_allowed' }))
    fireEvent.click(screen.getByTestId('message-action-copy'))
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith('note link'))
    expect(screen.queryByTestId('message-action-open')).toBeNull()
    from(el, 'result.offer', { invocationId: id, seq: 4, kind: 'open', value: 'https://notes.example.com/n/1' })
    const open = screen.getByTestId('message-action-open')
    expect(open.textContent).toContain('notes.example.com')
    fireEvent.click(open)
    expect(openExternal).toHaveBeenCalledWith('https://notes.example.com/n/1')
  })

  it('does not mark a replacement offer copied when the earlier write settles late', async () => {
    const { el } = await openFrame()
    from(el, 'ready')
    const id = invokeOf(el).invocationId
    from(el, 'result.offer', { invocationId: id, seq: 1, kind: 'copy', value: 'A' })
    let finish: (ok: boolean) => void = () => {}
    copyToClipboard.mockImplementationOnce(() => new Promise<boolean>((resolve) => { finish = resolve }))
    fireEvent.click(screen.getByTestId('message-action-copy'))
    expect(copyToClipboard).toHaveBeenCalledWith('A')
    from(el, 'result.offer', { invocationId: id, seq: 2, kind: 'copy', value: 'B' })
    await act(async () => { finish(true) })
    expect(screen.getByTestId('message-action-copy').textContent).toBe('Copy')
    fireEvent.click(screen.getByTestId('message-action-copy'))
    await waitFor(() => expect(screen.getByTestId('message-action-copy').textContent).toBe('Copied'))
    expect(copyToClipboard).toHaveBeenLastCalledWith('B')
  })

  it('stays open for a press on a menu, closes for a press elsewhere', async () => {
    const onClose = vi.fn()
    await openFrame(onClose)
    const menu = document.createElement('div')
    menu.setAttribute('role', 'menu')
    const item = document.createElement('div')
    menu.appendChild(item)
    const trigger = document.createElement('button')
    trigger.setAttribute('aria-haspopup', 'menu')
    document.body.append(menu, trigger)
    fireEvent.pointerDown(item)
    fireEvent.pointerDown(trigger)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.pointerDown(document.body)
    expect(onClose).toHaveBeenCalledWith('closed')
  })

  it('never grows past the viewport, and says so when the clipboard refuses', async () => {
    const { el } = await openFrame()
    expect(screen.getByTestId('message-action-popover').style.maxHeight).toBe('calc(100vh - 16px)')
    from(el, 'ready')
    const id = invokeOf(el).invocationId
    from(el, 'result.offer', { invocationId: id, seq: 1, kind: 'copy', value: 'x' })
    copyToClipboard.mockResolvedValueOnce(false)
    fireEvent.click(screen.getByTestId('message-action-copy'))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain("Couldn't copy"))
  })

  it('cancels the open invocation when the popover closes', async () => {
    const { el, view } = await openFrame()
    from(el, 'ready')
    const id = invokeOf(el).invocationId
    const sent = sentTo(el)
    view.unmount()
    expect(sent.find((m) => m.type === 'invoke.cancel')).toEqual(expect.objectContaining({ invocationId: id, reason: 'closed' }))
    expect(sent.at(-1)).toEqual(expect.objectContaining({ type: 'dispose' }))
    expect(document.querySelector('iframe[data-message-action-frame]')).toBeNull()
  })

  it('ends the invocation at its deadline, which each resize extends', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const onClose = vi.fn()
    const { el } = await openFrame(onClose)
    from(el, 'ready')
    const id = invokeOf(el).invocationId
    act(() => { vi.advanceTimersByTime(INVOCATION_DEADLINE_MS - 1000) })
    from(el, 'invocation.resize', { invocationId: id, width: 300, height: 9999 })
    act(() => { vi.advanceTimersByTime(INVOCATION_DEADLINE_MS - 1000) })
    expect(onClose).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(2000) })
    expect(onClose).toHaveBeenCalledWith('deadline')
  })

  it('says the app did not respond when the frame never gets ready', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { el } = await openFrame()
    act(() => { vi.advanceTimersByTime(READY_TIMEOUT_MS + 10) })
    expect(screen.getByTestId('message-action-failed').textContent).toContain('Notes')
    from(el, 'ready')
    expect(invokeOf(el)).toBeUndefined()
  })

  it('ignores messages from any other window, and a close for another invocation', async () => {
    const onClose = vi.fn()
    const { el } = await openFrame(onClose)
    act(() => postFrom({ [APP_FRAME_TAG]: 1, type: 'ready' }, window))
    expect(invokeOf(el)).toBeUndefined()
    from(el, 'ready')
    from(el, 'invocation.close', { invocationId: 'other' })
    expect(onClose).not.toHaveBeenCalled()
    from(el, 'invocation.close', { invocationId: invokeOf(el).invocationId })
    expect(onClose).toHaveBeenCalledWith('closed')
  })
})
