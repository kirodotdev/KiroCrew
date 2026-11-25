/**
 * App widgets: the `use` tag reaches `AppWidget`, a resolvable widget mounts the
 * shared sandbox with no capability and gets only its payload, and every other case
 * draws the fallback card with its reason.
 */
import type React from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { APP_FRAME_TAG } from '../appFrame/frameProtocol'
import { FRAME_SANDBOX } from '../appFrame/sandboxFrame'
import { installFrameWindows, postFrom, sentTo, type Sent } from '../test/LinkPreviewFramesTestUtils'
import { parseBlocks } from '../hooks/useBlockAssembler'
import AppWidget, { MAX_HEIGHT, READY_TIMEOUT_MS, setAppWidgetMintForTest } from './AppWidget'
import { MAX_WIDGET_PROPS_BYTES, parseWidgetProps, parseWidgetUse, resolveAppWidget } from './appWidgetRegistry'

const listApps = vi.fn()
vi.mock('../api/client', () => ({ api: { listApps: () => listApps() } }))

const app = (over: Record<string, unknown> = {}) => ({
  name: 'issue-radar',
  displayName: 'Issue Radar',
  enabled: true,
  manifest: { contributes: { widgets: [{ id: 'issue-card', title: 'Issue card', entry: 'widgets/card.mjs' }] } },
  ...over,
})

function withQuery(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>
}

describe('appWidgetRegistry', () => {
  it('reads use as <app>:<id> and nothing else', () => {
    expect(parseWidgetUse('issue-radar:issue-card')).toEqual({ appName: 'issue-radar', widgetId: 'issue-card' })
    for (const bad of ['issue-radar', 'Issue:card', 'a:B', 'a:../x', 'a:b:c', ':b', 'a:', '-a:b']) {
      expect(parseWidgetUse(bad)).toBeNull()
    }
  })

  it('resolves a declared widget of an enabled app, and says why not otherwise', () => {
    const ref = { appName: 'issue-radar', widgetId: 'issue-card' }
    expect(resolveAppWidget([app()], ref)).toEqual({ ...ref, appLabel: 'Issue Radar', title: 'Issue card' })
    expect(resolveAppWidget([], ref)).toBe('no-app')
    expect(resolveAppWidget([app({ enabled: false })], ref)).toBe('no-app')
    expect(resolveAppWidget([app({ manifest: { contributes: {} } })], ref)).toBe('no-widget')
    expect(resolveAppWidget([app()], { ...ref, widgetId: 'other' })).toBe('no-widget')
  })

  it('takes one JSON object within the cap as the payload', () => {
    expect(parseWidgetProps(' {"n": 1} ')).toEqual({ ok: true, props: { n: 1 } })
    expect(parseWidgetProps('[1]')).toEqual({ ok: false, reason: 'not-object' })
    expect(parseWidgetProps('null')).toEqual({ ok: false, reason: 'not-object' })
    expect(parseWidgetProps('<b>hi</b>')).toEqual({ ok: false, reason: 'not-object' })
    const big = JSON.stringify({ s: 'x'.repeat(MAX_WIDGET_PROPS_BYTES) })
    expect(parseWidgetProps(big)).toEqual({ ok: false, reason: 'too-large' })
  })

  it('the block parser carries use on the widget block, as widget_parse does', () => {
    const blocks = parseBlocks('<mcwidget use="issue-radar:issue-card" title="Issue 7">\n{"number": 7}\n</mcwidget>\n<mcwidget title="Page"><b>hi</b></mcwidget>', false)
    const widgets = blocks.filter((b) => b.type === 'widget')
    expect(widgets.map((b) => [b.use, b.language, b.content])).toEqual([
      ['issue-radar:issue-card', 'Issue 7', '{"number": 7}'],
      [undefined, 'Page', '<b>hi</b>'],
    ])
  })
})

describe('AppWidget', () => {
  beforeAll(() => installFrameWindows())
  const mint = vi.fn(async () => ({ frame: '/app-frame/issue-radar/widgets/issue-card/fid/tok', frameId: 'fid', capability: null, ttl: 900 }))
  beforeEach(() => {
    setAppWidgetMintForTest(mint)
    mint.mockClear()
    listApps.mockReset()
    listApps.mockResolvedValue([app()])
  })
  afterEach(() => {
    setAppWidgetMintForTest()
    vi.useRealTimers()
  })

  const from = (el: HTMLIFrameElement, type: string, data: Sent = {}) =>
    act(() => postFrom({ [APP_FRAME_TAG]: 1, type, ...data }, el.contentWindow))

  async function mounted(body = '{"number": 7, "title": "Chart fences"}') {
    render(withQuery(<AppWidget use="issue-radar:issue-card" body={body} title="Issue 7" />))
    const el = await waitFor(() => {
      const f = document.querySelector<HTMLIFrameElement>('iframe[data-app-widget-frame]')
      if (!f) throw new Error('no frame yet')
      return f
    })
    return el
  }

  it('mounts the shared sandbox with no capability and hands over only the payload', async () => {
    const el = await mounted()
    expect(mint).toHaveBeenCalledWith('issue-radar', 'issue-card')
    expect(el.getAttribute('sandbox')).toBe(FRAME_SANDBOX)
    expect(el.getAttribute('src')).toBe('/app-frame/issue-radar/widgets/issue-card/fid/tok')
    // The test DOM fires the frame's first load itself.
    await waitFor(() => expect(sentTo(el)[0]).toMatchObject({ type: 'init', capability: null }))
    from(el, 'ready')
    const renders = sentTo(el).filter((m) => m.type === 'widget.render')
    expect(renders).toHaveLength(1)
    expect(renders[0]).toMatchObject({ props: { number: 7, title: 'Chart fences' }, widgetId: 'issue-card' })
    // A second ready does not send the payload again.
    from(el, 'ready')
    expect(sentTo(el).filter((m) => m.type === 'widget.render')).toHaveLength(1)
  })

  it('takes the frame height it reports, within bounds', async () => {
    const el = await mounted()
    from(el, 'ready')
    from(el, 'widget.resize', { height: 5000 })
    const slot = screen.getByTestId('app-widget-slot')
    expect(slot.style.height).toBe(`${MAX_HEIGHT}px`)
    from(el, 'widget.resize', { height: 260 })
    expect(slot.style.height).toBe('260px')
  })

  it('draws the fallback card when the frame fails or never says ready', async () => {
    const el = await mounted()
    from(el, 'failed', { message: 'boom' })
    expect((await screen.findByTestId('app-widget-fallback')).dataset.reason).toBe('failed')
    expect(document.querySelector('iframe[data-app-widget-frame]')).toBeNull()
  })

  it('draws the fallback card on a frame that does not say ready in time', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await mounted()
    await act(async () => { vi.advanceTimersByTime(READY_TIMEOUT_MS + 1) })
    expect((await screen.findByTestId('app-widget-fallback')).dataset.reason).toBe('failed')
  })

  it.each([
    ['no-app', [], '{"n": 1}', 'issue-radar:issue-card'],
    ['no-widget', [app({ manifest: { contributes: { widgets: [] } } })], '{"n": 1}', 'issue-radar:issue-card'],
    ['not-object', [app()], 'not json', 'issue-radar:issue-card'],
    ['bad-use', [app()], '{"n": 1}', 'issue radar'],
  ])('draws the fallback card with its reason: %s', async (reason, apps, body, use) => {
    listApps.mockResolvedValue(apps)
    render(withQuery(<AppWidget use={use} body={body} title="Issue 7" />))
    const card = await screen.findByTestId('app-widget-fallback')
    expect(card.dataset.reason).toBe(reason)
    // The payload stays readable.
    expect(card.querySelector('pre')?.textContent).toBe(body)
    expect(mint).not.toHaveBeenCalled()
  })
})
