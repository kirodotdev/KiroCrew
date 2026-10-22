import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import type { Artifact } from '../types'

// A crewmate's weekly report or monthly review (`goal_report`, set by the gateway)
// opens in the crewmate Dashboard page's sandbox; every other html or widget
// artifact keeps the library frame it has today. Pinned on the full-page body and
// on the gallery thumbnail, the two places an artifact's html is rendered.

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'light', colorTheme: 'default', themeVersion: 0 }),
}))

vi.mock('../hooks/useCommentBridge', () => ({
  useCommentBridge: () => ({ scrollToAnchor: vi.fn() }),
}))

vi.mock('../hooks/useNearViewport', () => ({ useNearViewport: () => true }))

const { buildSrcdocSpy } = vi.hoisted(() => ({ buildSrcdocSpy: vi.fn() }))
vi.mock('../lib/widgetSrcdoc', async (importOriginal) => ({
  // The real constants (the sandbox grants); only rendering is stubbed.
  ...(await importOriginal<typeof import('../lib/widgetSrcdoc')>()),
  THEME_VAR_NAMES: [] as string[],
  readThemeVars: () => ({}) as Record<string, string>,
  buildSrcdoc: (opts: { html: string }) => {
    buildSrcdocSpy(opts)
    return opts.html
  },
}))

vi.mock('../api/client', () => ({
  api: { sandboxDocUrl: () => Promise.resolve({ url: '/sandbox-doc/abc/1.mac' }) },
  ApiError: class extends Error {},
}))

import { ArtifactBodyIframe } from '../components/ArtifactBody'
import { WidgetThumb } from '../components/library/ArtifactThumbs'
import { ARTIFACT_FRAME_SANDBOX, artifactFrame } from '../lib/artifactFrame'
import { CREW_DASHBOARD_SANDBOX } from '../pages/members/CrewDynamicDashboard'

/** The library frame: the agent-page grant, links through the host's bridge. */
const TODAY = 'allow-scripts'

function art(goal_report?: boolean): Artifact {
  return {
    slug: 'week-41', name: 'Week 41', kind: 'html', content: '<p>report</p>',
    ...(goal_report === undefined ? {} : { goal_report }),
  } as unknown as Artifact
}

async function frame(): Promise<HTMLIFrameElement> {
  let el: HTMLIFrameElement | null = null
  await waitFor(() => {
    el = document.querySelector('iframe')
    expect(el?.getAttribute('src')).toBeTruthy()
  })
  return el as unknown as HTMLIFrameElement
}

function lastSrcdocOptions(): Record<string, unknown> {
  return buildSrcdocSpy.mock.calls.at(-1)?.[0] as Record<string, unknown>
}

describe('artifactFrame', () => {
  it('gives a report the crewmate Dashboard page sandbox', () => {
    expect(artifactFrame(art(true))).toEqual({
      sandbox: CREW_DASHBOARD_SANDBOX, offlineScripts: true, linkBridge: false, rewriteBareLinks: false, pageScripts: false,
    })
  })

  it('leaves every other artifact on the library frame it has today', () => {
    expect(ARTIFACT_FRAME_SANDBOX).toBe(TODAY)
    for (const a of [art(), art(false), null, undefined]) {
      expect(artifactFrame(a)).toEqual({
        sandbox: TODAY, offlineScripts: false, linkBridge: true, rewriteBareLinks: false, pageScripts: true,
      })
    }
  })

  it('reads only a real true, so a stray truthy value cannot pick the frame', () => {
    const odd = { goal_report: 'yes' } as unknown as Artifact
    expect(artifactFrame(odd).sandbox).toBe(TODAY)
  })
})

describe('the artifact body frame', () => {
  beforeEach(() => buildSrcdocSpy.mockReset())

  it('opens a report with allow-scripts only and no CDN script origins', async () => {
    render(<ArtifactBodyIframe artifact={art(true)} slug="week-41" />)
    expect((await frame()).getAttribute('sandbox')).toBe('allow-scripts')
    const opts = lastSrcdocOptions()
    expect(opts.offlineScripts).toBe(true)
    expect(opts.rewriteBareLinks).toBe(false)
    // A report's links stay inert: no link bridge.
    expect(opts.linkBridge).toBe(false)
    expect(opts.pageScripts).toBe(false)
    // The host's own injected scripts stay on.
    expect(opts.includeHeightReporter).toBe(true)
    expect(opts.enableComments).toBe(true)
  })

  it('keeps an ordinary html artifact on today\'s frame', async () => {
    render(<ArtifactBodyIframe artifact={art()} slug="week-41" />)
    expect((await frame()).getAttribute('sandbox')).toBe(TODAY)
    const opts = lastSrcdocOptions()
    expect(opts.offlineScripts).toBe(false)
    expect(opts.linkBridge).toBe(true)
    expect(opts.pageScripts).toBe(true)
  })
})

describe('the gallery thumbnail frame', () => {
  beforeEach(() => buildSrcdocSpy.mockReset())

  it('previews a report in the same sandbox it opens in', async () => {
    render(<WidgetThumb content="<p>report</p>" slug="week-41" goalReport />)
    expect((await frame()).getAttribute('sandbox')).toBe('allow-scripts')
    expect(lastSrcdocOptions().offlineScripts).toBe(true)
  })

  it('keeps an ordinary thumbnail on today\'s frame', async () => {
    render(<WidgetThumb content="<p>report</p>" slug="week-41" goalReport={false} />)
    expect((await frame()).getAttribute('sandbox')).toBe(TODAY)
    expect(lastSrcdocOptions().offlineScripts).toBe(false)
  })
})
