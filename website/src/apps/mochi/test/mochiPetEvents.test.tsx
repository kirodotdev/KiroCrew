/**
 * Guards for three regressions that were all INVISIBLE in normal use:
 *  - the pet state machine was only ever reached with connect/disconnect, so
 *    every appearance pack showed one clip forever;
 *  - Mochi's markdown admitted no raw HTML, so a model writing `<br>` (the only
 *    line break available inside a GFM table cell) printed it literally;
 *  - the vendored widget frame pulled Tailwind from the public CDN, so widget
 *    styling depended on the network.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import { rehypeSanitize } from '../../../components/MarkdownRenderer'
import { buildSrcdoc } from '../../../lib/widgetSrcdoc'
import { TAILWIND_RUNTIME_PATH } from '../../../lib/vendorPaths'

describe('mochi markdown admits raw HTML, sanitized', () => {
  const plugins = { remarkPlugins: [remarkGfm], rehypePlugins: [rehypeRaw, rehypeSanitize] }

  it('renders <br> as a real break instead of literal text', () => {
    const { container } = render(<Markdown {...plugins}>{'one<br>two'}</Markdown>)
    expect(container.querySelectorAll('br')).toHaveLength(1)
    expect(container.textContent).not.toContain('<br>')
  })

  it('breaks inside a table cell survive (the common LLM case)', () => {
    const src = '| a | b |\n| --- | --- |\n| one<br>two | x |'
    const { container } = render(<Markdown {...plugins}>{src}</Markdown>)
    expect(container.querySelectorAll('td br')).toHaveLength(1)
  })

  it('admitting raw HTML does not admit script', () => {
    const { container } = render(
      <Markdown {...plugins}>{'<script>alert(1)</script>ok'}</Markdown>,
    )
    expect(container.querySelector('script')).toBeNull()
    expect(container.textContent).toContain('ok')
  })
})

describe('mochi widget frame uses the same-origin tailwind runtime', () => {
  it('srcdoc references no public CDN', () => {
    const srcdoc = buildSrcdoc({
      html: '<div class="p-2">hi</div>',
      themeVars: { '--bg': '#000' },
      mode: 'dark',
      includeHeightReporter: true,
    })
    expect(srcdoc).not.toContain('cdn.tailwindcss.com')
    expect(srcdoc).toContain(TAILWIND_RUNTIME_PATH)
  })
})

describe('panelBridge reports chat lifecycle to the pet state machine', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ agent: 'mochi' }) })
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => vi.unstubAllGlobals())

  const petEvents = () =>
    fetchMock.mock.calls
      .filter((c) => c[0] === '/api/apps/mochi/pet-event')
      .map((c) => JSON.parse(String((c[1] as RequestInit).body)).event)

  it('posts each event to the pet-event route', async () => {
    const bridge = await import('../panel/panelBridge')
    bridge.reportPetEvent('tool_call')
    expect(petEvents()).toEqual(['tool_call'])
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('same-origin')
  })

  it('a send reports user_input', async () => {
    const bridge = await import('../panel/panelBridge')
    await bridge.sendMessage('hello')
    expect(petEvents()).toContain('user_input')
  })

  it('reporting never rejects, so it cannot fail a send', async () => {
    fetchMock.mockRejectedValue(new Error('offline'))
    const bridge = await import('../panel/panelBridge')
    expect(() => bridge.reportPetEvent('task_complete')).not.toThrow()
  })
})
