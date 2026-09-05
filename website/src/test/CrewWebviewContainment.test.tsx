/**
 * Containment ratchet for a crew's webview.
 *
 * A crew fills this in on an unattended loop while reading issue bodies,
 * pull-request descriptions and review comments, so a path exists from a hostile
 * issue body to this component. There are now TWO views on that path and they
 * are contained by different mechanisms, which is why this file asserts both:
 *
 * - DOCKED is native React. Crew strings are text children, so they cannot
 *   become markup at all. Stronger than a sandbox, and the assertion is that no
 *   element appears from a payload that is trying to open one.
 * - EXPANDED is the sandboxed document, and its containment is one attribute.
 *   The frame is STRICTER than the artifact frame whose plumbing it borrows: the
 *   gateway route serves one CSP `sandbox` header for every consumer and that
 *   header grants popups — but sandbox restrictions COMBINE rather than union,
 *   so a grant the frame attribute withholds stays withheld. Asserting the exact
 *   attribute string is therefore asserting the effective sandbox.
 *
 * The third property here is the MINT COUNT, and it is the one a reader would
 * not guess. The minted URL is single-use server-side, so the frame is minted on
 * first expand and then kept mounted behind a hidden wrapper: zero mints docked,
 * one at first expand, still one after expand -> collapse -> expand.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const DOC_URL = '/sandbox-doc/panel123/1700000000.mac'
const PANEL_HTML = '<div id="kp-root"></div>'
const SLUG = 'fleet-crew'
/** The exact crew name; the read route matches it against the record's owner. */
const CREW = 'fleet-crew'

/** What a crew publishes. Order matters: `needs_you` is the urgent line and it
 *  is published FIRST, which is what the docked summary must lead with. */
const PANEL_DATA = {
  title: 'fleet-crew — cycle 47',
  subtitle: '5 workers held · 1 stalled · 34 of 100 credits',
  needs_you:
    'One ruling is waiting on you. Scope 758 asked a question 41 minutes ago and has stopped work.',
  cycle: 47,
  holding: 5,
  merged: 12,
  parked: 2,
}

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'dark', colorTheme: 'default', themeVersion: 0 }),
}))

vi.mock('../lib/widgetSrcdoc', () => ({
  THEME_VAR_NAMES: [] as string[],
  readThemeVars: () => ({}) as Record<string, string>,
  buildSrcdoc: (opts: { html: string }) => opts.html,
}))

const mintSpy = vi.fn()
vi.mock('../api/client', () => ({
  api: { sandboxDocUrl: (html: string) => mintSpy(html) },
  ApiError: class extends Error {},
}))

import CrewWebview, { CREW_WEBVIEW_SANDBOX } from '../pages/members/CrewWebview'

function panelResponse(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) })
}

function panelBody(data: Record<string, unknown> = PANEL_DATA) {
  return {
    panel: {
      template: 'default',
      title: 'fleet',
      crew: 'fleet-crew',
      published_at: '2026-09-04T05:16:00',
      data,
    },
    html: PANEL_HTML,
  }
}

describe('crew webview containment', () => {
  const originalCreate = globalThis.URL.createObjectURL
  const originalFetch = globalThis.fetch

  /** A fresh client per test: retries off so an error state is reached at once,
   *  and no cache carried between cases.
   *
   *  `member` is the exact crew name and the component requires it: the read route
   *  verifies it against the record's stored owner, so a colliding slug cannot be
   *  served another crew's panel. Defaulted here rather than threaded through every
   *  case, since only the ownership tests care which name it is. */
  function mount(slug = SLUG, member = CREW) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })
    return render(
      <QueryClientProvider client={client}>
        <CrewWebview slug={slug} member={member} />
      </QueryClientProvider>,
    )
  }

  beforeEach(() => {
    mintSpy.mockReset()
    mintSpy.mockResolvedValue({ url: DOC_URL })
    globalThis.fetch = vi.fn(() => panelResponse(panelBody())) as never
    // Any use of this for the frame is a regression to the form WebKit refuses.
    globalThis.URL.createObjectURL = vi.fn(() => {
      throw new Error('the crew webview must not use a blob: URL')
    }) as never
  })

  afterEach(() => {
    globalThis.URL.createObjectURL = originalCreate
    globalThis.fetch = originalFetch
  })

  /** Render and wait for the docked summary. No frame exists at this point. */
  async function renderDocked(): Promise<HTMLElement> {
    mount()
    let card: HTMLElement | null = null
    await waitFor(() => {
      card = document.querySelector('[data-testid="crew-webview-summary"]')
      expect(card).not.toBeNull()
    })
    return card as unknown as HTMLElement
  }

  /** Open the document and wait for the frame. */
  async function renderFrame(): Promise<HTMLIFrameElement> {
    await renderDocked()
    fireEvent.click(document.querySelector('[data-testid="crew-webview-expand"]') as Element)
    let frame: HTMLIFrameElement | null = null
    await waitFor(() => {
      frame = document.querySelector('iframe')
      expect(frame).not.toBeNull()
    })
    return frame as unknown as HTMLIFrameElement
  }

  // ------------------------------------------------------- the docked summary

  it('renders a native summary with no frame while docked', async () => {
    // The whole point of the rewrite: a dashboard needs a full page to be
    // legible, so the drawer shows a summary instead of a clipped document.
    await renderDocked()
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('costs nothing until the operator opens it', async () => {
    // A drawer opened on a crew whose dashboard is never read must not spend a
    // gateway round trip, and must not burn the single-use URL.
    await renderDocked()
    expect(mintSpy).not.toHaveBeenCalled()
  })

  it('leads with the first sentence-length field in published order', async () => {
    // Order is the only channel a crew has for saying what matters most. The
    // fixture publishes `needs_you` BEFORE four counters; an implementation that
    // bucketed by type would show the counters and push this below the fold,
    // which is the defect this assertion exists to prevent coming back.
    const card = await renderDocked()
    const lead = card.querySelector('[data-testid="crew-webview-lead"]')
    expect(lead).not.toBeNull()
    expect(lead?.textContent).toContain('One ruling is waiting on you')
    expect(lead?.textContent?.toLowerCase()).toContain('needs you')
  })

  it('shows the crew title and subtitle', async () => {
    const card = await renderDocked()
    expect(card.textContent).toContain('fleet-crew — cycle 47')
    expect(card.textContent).toContain('5 workers held')
  })

  it('offers a labelled control rather than a bare icon', async () => {
    // The affordance this replaces was a 14px icon at the far right of a bar
    // whose own text truncated mid-word, and a reviewer did not find it.
    const card = await renderDocked()
    const open = card.querySelector('[data-testid="crew-webview-expand"]')
    expect(open).not.toBeNull()
    expect(open?.textContent?.trim()).toBe('Open dashboard')
  })

  it('caps the docked stat rows so nothing has to be truncated', async () => {
    const card = await renderDocked()
    const rows = card.querySelectorAll('dl > div')
    expect(rows.length).toBeGreaterThan(0)
    expect(rows.length).toBeLessThanOrEqual(3)
  })

  it('renders a hostile crew string as text and injects no element', async () => {
    // The containment for the docked path. React escaping is what stands
    // between a hostile issue body and this card, and it is only containment for
    // as long as nobody reaches for dangerouslySetInnerHTML.
    const hostile = '</script><img src=x onerror="alert(1)"><script>'
    globalThis.fetch = vi.fn(() =>
      panelResponse(
        panelBody({
          title: hostile,
          subtitle: hostile,
          needs_you: `${hostile} and a sentence long enough to be read as prose here`,
          cycle: hostile,
        }),
      ),
    ) as never
    const card = await renderDocked()
    // Asserted by COUNTING elements: an escaped payload legitimately still
    // contains the TEXT `onerror=`, which is harmless with no tag around it.
    expect(card.querySelectorAll('img').length).toBe(0)
    expect(card.querySelectorAll('script').length).toBe(0)
    // ...and the value is not silently dropped either — it is shown, as text.
    expect(card.textContent).toContain('onerror=')
  })

  it('still names the crew when it published no title', async () => {
    const card = await renderDocked()
    expect(card.textContent).not.toBe('')
    globalThis.fetch = vi.fn(() => panelResponse(panelBody({ cycle: 1 }))) as never
    mount()
    await waitFor(() => {
      expect(document.body.textContent).toContain('fleet')
    })
  })

  // -------------------------------------------------------------- the frame

  it('grants the frame scripts and nothing else', async () => {
    const frame = await renderFrame()
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
  })

  it('keeps the exported constant and the rendered attribute in step', async () => {
    // The constant is what the module documents; the attribute is what the
    // browser enforces. A change to one that misses the other would leave the
    // documented reasoning describing a frame that no longer exists.
    const frame = await renderFrame()
    expect(CREW_WEBVIEW_SANDBOX).toBe('allow-scripts')
    expect(frame.getAttribute('sandbox')).toBe(CREW_WEBVIEW_SANDBOX)
  })

  it.each([
    'allow-same-origin',
    'allow-popups',
    'allow-popups-to-escape-sandbox',
    'allow-top-navigation',
    'allow-top-navigation-by-user-activation',
    'allow-forms',
    'allow-modals',
    'allow-downloads',
    'allow-pointer-lock',
    'allow-presentation',
    'allow-orientation-lock',
  ])('withholds %s', async grant => {
    const frame = await renderFrame()
    expect(frame.getAttribute('sandbox')).not.toContain(grant)
  })

  it('never omits the sandbox attribute entirely', async () => {
    // An absent attribute is an UNSANDBOXED frame, which reads as "no
    // restrictions listed" to a skimming eye and is the worst possible failure.
    const frame = await renderFrame()
    expect(frame.hasAttribute('sandbox')).toBe(true)
    expect(frame.getAttribute('sandbox')).not.toBe('')
  })

  it('addresses the minted document URL and builds no blob', async () => {
    const frame = await renderFrame()
    expect(frame.getAttribute('src')).toBe(DOC_URL)
    expect(mintSpy).toHaveBeenCalledWith(PANEL_HTML)
    expect(globalThis.URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('reads the crew it was given, not a caller-supplied target', async () => {
    await renderDocked()
    const called = (globalThis.fetch as unknown as { mock: { calls: string[][] } }).mock.calls
    const url = called[0][0]

    // The path names the slug it was handed, and the query names the EXACT crew.
    // Both matter: the slug picks the record, and the name is what the server
    // matches against the record's stored owner, so a colliding crew is not
    // served the other's panel.
    expect(url).toBe(
      `/api/members/${encodeURIComponent(SLUG)}/panel?member=${encodeURIComponent(CREW)}`,
    )
  })

  it('does not request a panel before it knows the exact crew name', async () => {
    // An empty name would reach the route as a missing parameter and 400. Nothing
    // is gained by asking; the drawer waits until it can ask correctly.
    mount(SLUG, '')
    await new Promise(r => setTimeout(r, 0))
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })

  it('renders nothing at all when the crew has filled in nothing', async () => {
    globalThis.fetch = vi.fn(() => panelResponse({ panel: null, html: null })) as never
    mount()
    await waitFor(() => {
      expect(document.querySelector('[data-testid="crew-webview-empty"]')).not.toBeNull()
    })
    expect(document.querySelector('iframe')).toBeNull()
    expect(document.querySelector('[data-testid="crew-webview-summary"]')).toBeNull()
    expect(mintSpy).not.toHaveBeenCalled()
  })

  it('shows an error state rather than an empty one when the read fails', async () => {
    // Three states, never conflated: a failed read must not render the
    // affirmative "this crew has not filled in its dashboard".
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    ) as never
    mount()
    await waitFor(() => {
      expect(document.querySelector('[data-testid="crew-webview-error"]')).not.toBeNull()
    })
    expect(document.querySelector('iframe')).toBeNull()
    expect(document.querySelector('[data-testid="crew-webview-empty"]')).toBeNull()
  })

  it('offers a way out of the read failure instead of a dead end', async () => {
    // The error state rendered text and nothing else, while the mint failure a
    // few lines below it offered "Try again" -- the same failure class with two
    // different answers, one of them unrecoverable without reselecting the crew.
    let attempts = 0
    globalThis.fetch = vi.fn(() => {
      attempts += 1
      if (attempts === 1) {
        return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) })
      }
      return panelResponse(panelBody())
    }) as never
    mount()
    await waitFor(() => {
      expect(document.querySelector('[data-testid="crew-webview-error-retry"]')).not.toBeNull()
    })
    fireEvent.click(document.querySelector('[data-testid="crew-webview-error-retry"]') as Element)
    // Recovers IN PLACE: the summary appears without the crew being reselected.
    await waitFor(() => {
      expect(document.querySelector('[data-testid="crew-webview-summary"]')).not.toBeNull()
    })
  })

  it('renders both failures through the shared error surface', async () => {
    // A hand-rolled `role="alert"` box loses the agent hand-off that every other
    // error on the dashboard offers, and drifts from it visually.
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    ) as never
    mount()
    const notice = await waitFor(() => {
      const n = document.querySelector('[data-testid="crew-webview-error"]')
      expect(n).not.toBeNull()
      return n as Element
    })
    expect(notice.getAttribute('role')).toBe('alert')
    // The hand-off is what distinguishes the shared component from a styled div.
    expect(notice.querySelector('button, a')).not.toBeNull()
  })

  // ---------------------------------------------------------------- expanding

  function host() {
    return document.querySelector('[data-testid="crew-webview"]')
  }

  async function waitExpanded(want: 'true' | 'false') {
    await waitFor(() => {
      expect(host()?.getAttribute('data-expanded')).toBe(want)
    })
  }

  it('keeps the identical sandbox grants when expanded', async () => {
    // The whole point of expanding is more room, not more capability.
    const frame = await renderFrame()
    await waitExpanded('true')
    expect(frame.getAttribute('sandbox')).toBe(CREW_WEBVIEW_SANDBOX)
  })

  it('mints exactly once across expand, collapse and expand again', async () => {
    // THE load-bearing test of this component. The minted URL is SINGLE-USE
    // server-side, so the frame must be minted on first expand and then KEPT
    // MOUNTED behind a hidden wrapper. A wrapper that unmounted it would
    // re-request a spent URL on the second expand and render a blank frame — a
    // failure that shows nothing in the DOM shape, only in this count.
    await renderDocked()
    expect(mintSpy).toHaveBeenCalledTimes(0)

    fireEvent.click(document.querySelector('[data-testid="crew-webview-expand"]') as Element)
    await waitExpanded('true')
    const first = document.querySelector('iframe')
    expect(mintSpy).toHaveBeenCalledTimes(1)

    fireEvent.click(document.querySelector('[data-testid="crew-webview-collapse"]') as Element)
    await waitExpanded('false')
    // Hidden, NOT unmounted — this is the assertion that pins the fix.
    expect(document.querySelector('iframe')).toBe(first)

    fireEvent.click(document.querySelector('[data-testid="crew-webview-expand"]') as Element)
    await waitExpanded('true')
    expect(mintSpy).toHaveBeenCalledTimes(1)
    expect(document.querySelector('iframe')).toBe(first)
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(DOC_URL)
  })

  it('collapses on Escape and keeps the loaded document', async () => {
    // A full-window overlay with no keyboard exit is a trap.
    await renderFrame()
    await waitExpanded('true')
    const frame = document.querySelector('iframe')
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitExpanded('false')
    expect(document.querySelector('iframe')).toBe(frame)
    expect(mintSpy).toHaveBeenCalledTimes(1)
  })

  it('returns to the summary when collapsed', async () => {
    await renderFrame()
    await waitExpanded('true')
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitExpanded('false')
    expect(document.querySelector('[data-testid="crew-webview-summary"]')).not.toBeNull()
  })

  it('marks the expanded view as a modal dialog', async () => {
    await renderFrame()
    await waitExpanded('true')
    expect(host()?.getAttribute('role')).toBe('dialog')
    expect(host()?.getAttribute('aria-modal')).toBe('true')
  })

  it('names the dialog after its content, not after an action', async () => {
    // `aria-label` was the collapse string, so a screen reader announced the
    // region as "Collapse the dashboard" -- which is what a button inside it
    // does, not what the region IS.
    await renderFrame()
    await waitExpanded('true')
    const label = host()?.getAttribute('aria-label') || ''
    expect(label).toContain('fleet-crew')
    expect(label.toLowerCase()).not.toContain('collapse')
  })

  it('moves focus into the dialog when it opens', async () => {
    // Opening unmounts the summary card, so the button just pressed disappears
    // and focus falls to <body> -- inside an aria-modal region, leaving a
    // keyboard user nowhere.
    await renderDocked()
    fireEvent.click(document.querySelector('[data-testid="crew-webview-expand"]') as Element)
    await waitExpanded('true')
    await waitFor(() => {
      expect(document.activeElement).toBe(
        document.querySelector('[data-testid="crew-webview-collapse"]'),
      )
    })
  })

  it('routes the expanded dialog through the shared focus trap', () => {
    // `aria-modal` HIDES the drawer, sidebar and composer from assistive tech
    // while leaving them tab-reachable, so an untrapped Tab walks a keyboard user
    // into controls their screen reader has stopped announcing. The hand-rolled
    // version here handled Escape and the focus hand-off but never trapped Tab.
    //
    // Asserted at SOURCE level, matching `apps/command-bar/rootIndex.test.ts`,
    // because the behaviour cannot be observed in jsdom: the hook filters its
    // focusables with `el.offsetParent !== null`, and jsdom performs no layout,
    // so `offsetParent` is always null and the trap is a guaranteed no-op there.
    // A behavioural assertion written anyway ("focus stayed inside the dialog")
    // passes with the hook DELETED -- it was vacuous, which is worse than absent.
    // The trap's own behaviour is covered by useDialogFocusTrap.imeGuard and
    // .restoreFocus; what belongs here is that this component uses it at all.
    const src = readFileSync(
      resolve(__dirname, '../pages/members/CrewWebview.tsx'),
      'utf8',
    )
    expect(src).toMatch(/useDialogFocusTrap\(dialogRef,/)
    // And that the trap is keyed to the expanded state: enabled while docked it
    // would trap Tab inside a card the user has not opened.
    expect(src).toMatch(/enabled:\s*expanded/)
    // The hand-rolled window-level Escape listener must not come back beside it.
    expect(src).not.toMatch(/addEventListener\(\s*'keydown'/)
  })

  it('restores focus to the opening control when it collapses', async () => {
    await renderDocked()
    fireEvent.click(document.querySelector('[data-testid="crew-webview-expand"]') as Element)
    await waitExpanded('true')
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitExpanded('false')
    await waitFor(() => {
      expect(document.activeElement).toBe(
        document.querySelector('[data-testid="crew-webview-expand"]'),
      )
    })
  })

  it('does not steal focus merely by rendering while docked', async () => {
    // The restore must fire on a real collapse, not on first mount -- the drawer
    // renders this component whenever a crew is selected.
    const before = document.activeElement
    await renderDocked()
    expect(document.activeElement).toBe(before)
  })

  it('anchors the relative age with an absolute time', async () => {
    // A bare "23m" does not say 23 minutes before WHAT, and it goes stale while
    // the drawer sits open.
    //
    // Found by the tooltip rather than by matching the age text: the age now comes
    // from `fmtRelative`, so its wording is CLDR's and differs per locale ("23m
    // ago", "vor 23 Minuten"). Asserting a shape like /^\d+[smhd]$/ would pin the
    // English abbreviation this deliberately stopped hand-building.
    const card = await renderDocked()
    // Found by its OWN test id, not by "the first span carrying a title": the
    // containment chip beside it also carries one now (its tooltip explains what
    // the shield asserts), and a positional locator silently picked that up
    // instead -- a green-to-red flip with no behaviour change behind it.
    const chip = card.querySelector('[data-testid="crew-webview-age"]')
    expect(chip, 'no age chip carrying a tooltip').toBeTruthy()

    // The tooltip is an absolute instant, which is the anchor the age lacks.
    const title = chip?.getAttribute('title') || ''
    expect(Number.isFinite(new Date(title).getTime())).toBe(true)
    // And it is not simply the same relative string twice over.
    expect(title).not.toBe((chip?.textContent || '').trim())
  })

  it('is not a dialog while docked', async () => {
    await renderDocked()
    expect(host()?.getAttribute('role')).toBeNull()
  })
})
