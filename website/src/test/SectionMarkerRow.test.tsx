/**
 * Section markers render as a labelled rule on BOTH transcript surfaces.
 *
 * A parity test, not just a component test: the two paths disagree on the
 * unknown-role fallback — the single-chat role chain draws an assistant bubble,
 * the SDK registry draws nothing — so a row taught to only one is silently wrong
 * on the other, and both failures look fine from wherever you were looking.
 *
 * The role-parity contract test already fails when one path claims the role and
 * the other does not. What it cannot see is whether the row DRAWS a rule.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import SectionMarkerRow from '../pages/chat/SectionMarkerRow'
import {
  defaultMessageRenderers,
  formatTsPrecise,
  resolveRenderer,
  type MessageRenderContext,
} from '../app-sdk/messageRenderers'
import type { ChatMessage } from '../types'

const here = dirname(fileURLToPath(import.meta.url))

const marker = (over: Partial<ChatMessage> = {}): ChatMessage =>
  ({
    role: 'section_marker',
    content: '— End of section: second-item —',
    cls: '',
    meta: { label: 'second-item' },
    ...over,
  }) as ChatMessage

describe('SectionMarkerRow', () => {
  it('draws the label between two hairline rules', () => {
    const { container } = render(<SectionMarkerRow label="second-item" />)
    const row = container.querySelector('[data-testid="section-marker-row"]')
    expect(row).not.toBeNull()
    expect(row!.textContent).toBe('End of section: second-item')
    // Two rules, one either side, so the label reads as a break rather than as a
    // heading with a trailing line.
    expect(container.querySelectorAll('span.h-px').length).toBe(2)
  })

  it('is a separator for assistive tech, named by its label', () => {
    const { container } = render(<SectionMarkerRow label="second-item" />)
    const row = container.querySelector('[role="separator"]')
    expect(row).not.toBeNull()
    expect(row!.getAttribute('aria-label')).toBe('End of section: second-item')
  })

  it('renders exactly the name the capture script asserts before it shoots', () => {
    // The script throws when the composed name differs, stopping every frame, so
    // reading its literal fails here instead — in jsdom, with no browser.
    const script = readFileSync(
      resolve(here, '../../scripts/capture-transcript-section-markers.mjs'),
      'utf8',
    )
    const pattern = /const shape = \/(.+)\/\n/.exec(script)?.[1]
    expect(pattern, 'the capture script no longer pins a composed name').toBeTruthy()
    const shape = new RegExp(pattern!)
    // Today, as the scene builds its instants: the shipped stamp gains a date off-day,
    // so a fixed past date would test a format the captured frames never show.
    const sameDay = new Date()
    sameDay.setHours(14, 18, 32, 0)
    const { container } = render(
      <SectionMarkerRow label="item-42" time={formatTsPrecise(sameDay.toISOString())} />,
    )
    const row = container.querySelector('[data-testid="section-marker-row"]')
    expect(shape.test(row!.getAttribute('aria-label') || '')).toBe(true)
  })

  it('tells two same-label breaks apart by the instant each was requested', () => {
    const first = render(<SectionMarkerRow label="item-42" time="2:38 PM" />)
    const second = render(<SectionMarkerRow label="item-42" time="2:41 PM" />)
    const nameOf = (r: ReturnType<typeof render>) =>
      r.container.querySelector('[role="separator"]')!.getAttribute('aria-label')
    expect(nameOf(first)).toBe('End of section: item-42 · 2:38 PM')
    expect(nameOf(second)).toBe('End of section: item-42 · 2:41 PM')
    expect(nameOf(first)).not.toBe(nameOf(second))
  })

  it('names an unlabelled break in the reader language, not the backend one', () => {

    // `label=""` with that `content` is exactly what the applier sends for an
    // unlabelled marker, so this path must not draw its raw English text.
    const { container } = render(<SectionMarkerRow label="" fallback="— End of section —" />)
    const row = container.querySelector('[data-testid="section-marker-row"]')
    expect(row).not.toBeNull()
    expect(row!.textContent).toBe('End of section')
    expect(row!.textContent).not.toContain('—')
    expect(container.querySelectorAll('span.h-px').length).toBe(2)
  })

  it('distinguishes two unlabelled breaks by stamping each with its own time', () => {
    // Every unlabelled break draws the same caption, so the time is the only
    // thing separating one from the next.
    const { container } = render(<SectionMarkerRow label="" time="2:32 PM" />)
    const row = container.querySelector('[data-testid="section-marker-row"]')
    expect(row).not.toBeNull()
    expect(row!.textContent).toContain('End of section')
    expect(container.querySelector('[data-testid="section-marker-time"]')!.textContent).toBe('2:32 PM')
    expect(row!.getAttribute('aria-label')).toBe('End of section · 2:32 PM')
  })

  it('stamps a LABELLED break too, since the same label can be marked twice', () => {
    const { container } = render(<SectionMarkerRow label="second-item" time="2:32 PM" />)
    const row = container.querySelector('[data-testid="section-marker-row"]')
    expect(container.querySelector('[data-testid="section-marker-time"]')!.textContent).toBe('2:32 PM')
    expect(row!.textContent).toContain('End of section: second-item')
    expect(row!.getAttribute('aria-label')).toBe('End of section: second-item · 2:32 PM')
  })

  it('carries the full timestamp as a hover title while the visible stamp stays short', () => {
    const { container } = render(
      <SectionMarkerRow label="second-item" time="2:32 PM" timeTitle="Mar 20, 2026, 2:32:07 PM" />,
    )
    const stamp = container.querySelector('[data-testid="section-marker-time"]')!
    expect(stamp.getAttribute('title')).toBe('Mar 20, 2026, 2:32:07 PM')
    expect(stamp.textContent).toBe('2:32 PM')
  })

  it('emits no title attribute at all when no full timestamp is supplied', () => {
    const { container } = render(<SectionMarkerRow label="second-item" time="2:32 PM" />)
    const stamp = container.querySelector('[data-testid="section-marker-time"]')!
    expect(stamp.hasAttribute('title')).toBe(false)
  })

  it('documents why a structural row ships no dismiss control, and ships none', () => {
    const component = readFileSync(resolve(here, '../pages/chat/SectionMarkerRow.tsx'), 'utf8')
    expect(component).toMatch(/No dismiss affordance, deliberately/)
    const { container } = render(<SectionMarkerRow label="second-item" time="2:32 PM" />)
    expect(container.querySelector('button')).toBeNull()
  })

  it('keeps the unlabelled caption on one framing in every shipped locale', () => {
    // Both captions name a BOUNDARY. A locale left on the retired divider framing
    // makes the same rule read as two different things to two readers.
    const superseded = [
      'Section break',
      'Abschnittsumbruch',
      'Salto de sección',
      'Saut de section',
      'Interruzione di sezione',
      'Quebra de seção',
      'Разрыв раздела',
      'セクション区切り',
      '섹션 나누기',
      '分节符',
      'अनुभाग विराम',
      'বিভাগ বিরতি',
    ]
    const dir = resolve(here, '../i18n/locales')
    const stale: string[] = []
    let checked = 0
    for (const file of readdirSync(dir).filter(f => f.endsWith('.json'))) {
      const data = JSON.parse(readFileSync(resolve(dir, file), 'utf8'))
      const caption = data?.pages?.chat?.sectionMarkerRow?.end_of_section
      if (typeof caption !== 'string') continue
      checked += 1
      if (superseded.includes(caption)) stale.push(`${file}: ${caption}`)
    }
    // Positive control: the sweep has to actually reach the catalogs.
    expect(checked).toBeGreaterThan(10)
    expect(stale).toEqual([])
  })

  it('names a boundary whether the break is labelled or not', () => {
    // A labelled break said "End of section: x" while an unlabelled one said "Section
    // break": one named a boundary of the work above, the other a neutral divider.
    const captionOf = (node: React.ReactElement) => {
      const { container } = render(node)
      return container.querySelector('[data-testid="section-marker-row"]')!.textContent!
    }
    const labelled = captionOf(<SectionMarkerRow label="second-item" />)
    const unlabelled = captionOf(<SectionMarkerRow label="" />)
    // Both open on the same boundary word, which is what ties them together.
    expect(labelled.startsWith('End of')).toBe(true)
    expect(unlabelled.startsWith('End of')).toBe(true)
    // Positive control: they are still DIFFERENT captions, so this is not
    // passing because both resolved to one fallback string.
    expect(labelled).not.toBe(unlabelled)
    expect(labelled).toContain('second-item')
  })

  it('keeps two breaks carrying the SAME label distinguishable', () => {
    // An agent that retries a unit of work marks it twice with one label, so the
    // caption cannot separate them and only the time does.
    const first = render(<SectionMarkerRow label="Ticket 4132" time="2:32 PM" />)
    const second = render(<SectionMarkerRow label="Ticket 4132" time="4:10 PM" />)
    const nameOf = (r: ReturnType<typeof render>) =>
      r.container.querySelector('[role="separator"]')!.getAttribute('aria-label')
    expect(nameOf(first)).not.toBe(nameOf(second))
    const textOf = (r: ReturnType<typeof render>) =>
      r.container.querySelector('[data-testid="section-marker-row"]')!.textContent
    expect(textOf(first)).not.toBe(textOf(second))
    // Positive control: both really do carry the shared label, so this is not
    // passing because one of them failed to render its caption.
    expect(textOf(first)).toContain('Ticket 4132')
    expect(textOf(second)).toContain('Ticket 4132')
  })

  it('stamps an older-client row as well, so its break is separable too', () => {
    const { container } = render(<SectionMarkerRow fallback="— End of section —" time="2:32 PM" />)
    const row = container.querySelector('[data-testid="section-marker-row"]')
    expect(container.querySelector('[data-testid="section-marker-time"]')!.textContent).toBe('2:32 PM')
    expect(row!.textContent).toContain('— End of section —')
  })

  it('still draws a plain break for a row with neither meta nor content', () => {
    const { container } = render(<SectionMarkerRow />)
    const row = container.querySelector('[data-testid="section-marker-row"]')
    expect(row).not.toBeNull()
    expect(row!.textContent).toBe('End of section')
    expect(container.querySelectorAll('span.h-px').length).toBe(2)
    // Named, because a caption-less pair of rules reads as a rendering artifact
    // and announces as a nameless separator.
    expect(row!.getAttribute('aria-label')).toBe('End of section')
  })

  it('lets a long label wrap instead of overflowing the column', () => {
    // `shrink-0` sizes the span to max-content, so `break-words` never gets a
    // constrained box and a long label overflows the column instead of wrapping.
    const { container } = render(<SectionMarkerRow label={'wrap-me-'.repeat(14)} />)
    const span = container.querySelector('[data-testid="section-marker-row"] > span:not([aria-hidden])')
    expect(span).not.toBeNull()
    expect(span!.className).not.toContain('shrink-0')
    expect(span!.className).toContain('break-words')
    expect(span!.className).toContain('min-w-0')
  })

  it('renders the label as text, never as markup', () => {
    const { container } = render(<SectionMarkerRow label="<img src=x onerror=1>" />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('<img src=x onerror=1>')
  })
})

describe('the SDK path draws the rule', () => {
  it('titles the row with its own stamp and never a second instant', () => {
    const rows: React.ReactNode[] = []
    const ctx = { row: (node: React.ReactNode) => rows.push(node) } as never
    // Metadata still CARRYING a stale requested instant is the regression case: the
    // renderer must ignore it rather than resurface the pair this PR removed.
    const m = marker({
      ts: '2026-03-20T14:41:09Z',
      meta: { label: 'item-42', at: '2026-03-20T14:38:07Z' },
    })
    const entry = resolveRenderer(m, defaultMessageRenderers)
    entry!.render(m, ctx)
    const { container } = render(<>{rows}</>)
    const title = container.querySelector('[title]')!.getAttribute('title')!
    expect(title).not.toContain(' · ')
    expect(title).not.toMatch(/14:38|2:38/)
    expect(title).toMatch(/2:41/)
  })

  it('resolves a section_marker row to its own registry entry', () => {
    // Not `undrawn` and not `undefined`: both draw nothing on screen, so only an
    // entry of its own is correct.
    expect(resolveRenderer(marker(), defaultMessageRenderers)?.id).toBe('section_marker')
  })

  it('renders the rule with the label from meta', () => {
    const entry = resolveRenderer(marker(), defaultMessageRenderers)!
    const ctx = { row: (node: React.ReactNode) => node, messages: [marker()] } as unknown as MessageRenderContext
    const { container } = render(<>{entry.render(marker(), ctx)}</>)
    const row = container.querySelector('[data-testid="section-marker-row"]')
    expect(row).not.toBeNull()
    expect(row!.textContent).toBe('End of section: second-item')
  })

  it('wires the full timestamp through from the row, not just the short stamp', () => {
    // The registry entry is the only place the two forms are paired, so the
    // component test cannot prove the caller supplies the longer one.
    const m = marker({ ts: '2026-03-20T14:32:07Z' })
    const entry = resolveRenderer(m, defaultMessageRenderers)!
    const ctx = { row: (node: React.ReactNode) => node, messages: [m, marker({ ts: '2026-03-20T15:01:00Z' })] } as unknown as MessageRenderContext
    const { container } = render(<>{entry.render(m, ctx)}</>)
    const stamp = container.querySelector('[data-testid="section-marker-time"]')!
    const title = stamp.getAttribute('title') ?? ''
    expect(title).not.toBe('')
    expect(title.length).toBeGreaterThan(stamp.textContent!.length)
  })

  it('gives the stamp the caption weight, since it alone separates twinned breaks', () => {
    const a = marker({ ts: '2026-03-20T14:41:00Z' })
    const b = marker({ ts: '2026-03-20T15:07:00Z' })
    const entry = resolveRenderer(a, defaultMessageRenderers)!
    const ctx = { row: (node: React.ReactNode) => node, messages: [a, b] } as unknown as MessageRenderContext
    const { container } = render(<>{entry.render(a, ctx)}</>)
    const stamp = container.querySelector('[data-testid="section-marker-time"]')!
    const caption = [...container.querySelectorAll('span')].find(
      (s) => s !== stamp && s.textContent?.includes('End of'),
    )!
    const weightOf = (el: Element) =>
      (el.className.match(/font-(\w+)/) ?? [])[1] ?? ''
    expect(weightOf(caption)).not.toBe('')
    expect(weightOf(stamp)).toBe(weightOf(caption))
  })

  it('falls back to content when meta carries no label', () => {
    // An older producer, or a row whose meta was dropped, still reads legibly
    // rather than as a blank rule. `content` is the compatibility surface.
    const m = marker({ meta: undefined })
    const entry = resolveRenderer(m, defaultMessageRenderers)!
    const ctx = { row: (node: React.ReactNode) => node, messages: [m] } as unknown as MessageRenderContext
    const { container } = render(<>{entry.render(m, ctx)}</>)
    expect(container.textContent).toBe('— End of section: second-item —')
  })
})

describe('ChatPage draws the rule too', () => {
  /**
   * ChatPage no longer carries a per-role if-chain: every transcript row resolves
   * through the SAME renderer registry the other surfaces consume, so the role needs
   * no branch on this page at all. What still has to hold is the CHAIN that gets the
   * row here -- the page merges the defaults, the defaults claim the role, and no host
   * entry shadows it -- so that is what these assert. A source match on a branch would
   * now pin the old architecture and pass while the row rendered as a stray bubble.
   */
  const src = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
  const registry = readFileSync(resolve(here, '../app-sdk/messageRenderers.tsx'), 'utf8')

  it('dispatches through the registry that claims the role', () => {
    // The page resolves rows against a merged registry rather than its own chain.
    expect(/mergeRenderers\(/.test(src)).toBe(true)
    expect(/resolveRenderer\(/.test(src)).toBe(true)
    // A HOST entry re-using this id would shadow the default and silently drop the
    // row -- the one way this page can still lose the marker.
    expect(src).not.toMatch(/id:\s*'section_marker'/)
    expect(registry).toMatch(/id:\s*'section_marker'/)
    expect(registry).toMatch(/roles:\s*\[\s*'section_marker'\s*\]/)
  })

  it('renders the same component the SDK path uses, and only there', () => {
    // One entry in one registry is what stops two components drawing "a labelled
    // rule" from drifting, so the import must not be duplicated on the page.
    expect(/import SectionMarkerRow from '\.\.\/pages\/chat\/SectionMarkerRow'/.test(registry)).toBe(true)
    expect(/<SectionMarkerRow\b/.test(registry)).toBe(true)
    expect(src).not.toMatch(/<SectionMarkerRow\b/)
  })

  it('prefers meta.label over content in the entry the page resolves', () => {
    // Whitespace-tolerant: a formatter re-wrapping the JSX must not red a test
    // about which field is read.
    const flat = registry.replace(/\s+/g, ' ')
    expect(flat).toMatch(/const label = m\.meta\?\.label as string/)
    expect(flat).toMatch(/<SectionMarkerRow[^>]*label=\{label\}/)
    expect(flat).toMatch(/<SectionMarkerRow[^>]*fallback=\{m\.content\}/)
  })

  describe('a malformed persisted row degrades instead of crashing', () => {
    // Both call sites CAST `meta.label` to string, and a cast erases nothing at
    // runtime: these values really can arrive from the history API.
    const notStrings: [string, unknown][] = [
      ['a number', 5],
      ['an object', { toString: undefined }],
      ['an array', ['a', 'b']],
      ['a boolean', true],
    ]

    for (const [what, value] of notStrings) {
      it(`renders when the label is ${what}`, () => {
        const { container } = render(<SectionMarkerRow label={value as never} />)
        const row = container.querySelector('[data-testid="section-marker-row"]')
        expect(row).not.toBeNull()
        // Degrades to the generic caption: the row HAS a label field, so it is
        // not the older-client case that draws the raw fallback.
        expect(row?.textContent).toBe('End of section')
      })

      it(`renders when the content fallback is ${what}`, () => {
        const { container } = render(<SectionMarkerRow fallback={value as never} />)
        expect(container.querySelector('[data-testid="section-marker-row"]')).not.toBeNull()
      })
    }

    it('keeps the whole SDK transcript path alive on a malformed row', () => {
      const bad = marker({ meta: { label: { nested: true } } } as Partial<ChatMessage>)
      const ctx = { row: (node: React.ReactNode) => node, messages: [bad] } as unknown as MessageRenderContext
      const entry = resolveRenderer(bad, defaultMessageRenderers)!
      expect(entry.id).toBe('section_marker')
      const { container } = render(<>{entry.render(bad, ctx)}</>)
      expect(container.querySelector('[data-testid="section-marker-row"]')).not.toBeNull()
    })
  })
})

describe('UX: a break never draws a nameless rule, and every break carries its time', () => {
  it('captions a row that carries neither meta nor content', () => {
    // Older client, meta dropped: without the floor this drew two bare rules with
    // no caption and no accessible name, reading as a rendering artifact.
    const { container } = render(<SectionMarkerRow fallback="" />)
    const row = container.querySelector('[data-testid="section-marker-row"]')
    expect(row).not.toBeNull()
    expect(row!.textContent).toBe('End of section')
    expect(row!.getAttribute('aria-label')).toBe('End of section')
  })

  it('stamps a labelled break whose label is unique, like every other break', () => {
    const m = marker({ ts: '2026-03-20T14:32:07Z' })
    const entry = resolveRenderer(m, defaultMessageRenderers)!
    const ctx = {
      row: (node: React.ReactNode) => node,
      messages: [m],
    } as unknown as MessageRenderContext
    const { container } = render(<>{entry.render(m, ctx)}</>)
    expect(container.querySelector('[data-testid="section-marker-time"]')).not.toBeNull()
    expect(container.textContent).toContain('End of section: second-item')
  })

  it('stamps every break the same way, so a stamp never signals significance', () => {
    // The three shapes together: unique label, twinned label, no label. A stamp on
    // some rules and not others read as "this break matters".
    const unique = marker({ ts: '2026-03-20T14:32:07Z' })
    const twin = marker({ ts: '2026-03-20T15:01:00Z' })
    const bare = marker({ meta: { label: '' }, ts: '2026-03-20T15:30:00Z' })
    const entry = resolveRenderer(unique, defaultMessageRenderers)!
    for (const m of [unique, twin, bare]) {
      const ctx = {
        row: (node: React.ReactNode) => node,
        messages: [unique, twin, bare],
      } as unknown as MessageRenderContext
      const { container } = render(<>{entry.render(m, ctx)}</>)
      expect(container.querySelector('[data-testid="section-marker-time"]')).not.toBeNull()
    }
  })

  it('keeps the stamp when the same label is marked twice', () => {
    const m = marker({ ts: '2026-03-20T14:32:07Z' })
    const twin = marker({ ts: '2026-03-20T15:01:00Z' })
    const entry = resolveRenderer(m, defaultMessageRenderers)!
    const ctx = {
      row: (node: React.ReactNode) => node,
      messages: [m, twin],
    } as unknown as MessageRenderContext
    const { container } = render(<>{entry.render(m, ctx)}</>)
    expect(container.querySelector('[data-testid="section-marker-time"]')).not.toBeNull()
  })

  it('keeps the stamp on an unlabelled break, which has no label to tell apart', () => {
    const m = marker({ meta: { label: '' }, ts: '2026-03-20T14:32:07Z' })
    const entry = resolveRenderer(m, defaultMessageRenderers)!
    const ctx = {
      row: (node: React.ReactNode) => node,
      messages: [m],
    } as unknown as MessageRenderContext
    const { container } = render(<>{entry.render(m, ctx)}</>)
    expect(container.querySelector('[data-testid="section-marker-time"]')).not.toBeNull()
  })

  it('draws BOTH of two adjacent unlabelled breaks, since each was requested', () => {
    const first = marker({ meta: { label: '' }, ts: '2026-03-20T14:18:00Z' })
    const second = marker({ meta: { label: '' }, ts: '2026-03-20T14:19:00Z' })
    const entry = resolveRenderer(second, defaultMessageRenderers)!
    const ctxAt = (i: number) =>
      ({
        row: (node: React.ReactNode) => node,
        messages: [first, second],
        index: i,
      }) as unknown as MessageRenderContext
    for (const [i, m] of [first, second].entries()) {
      const { container } = render(<>{entry.render(m, ctxAt(i))}</>)
      expect(container.querySelector('[data-testid="section-marker-row"]')).not.toBeNull()
    }
  })

  it('draws an unlabelled break that follows an ordinary turn', () => {
    const prior = { role: 'assistant', content: 'work', ts: '2026-03-20T14:17:00Z' } as ChatMessage
    const m = marker({ meta: { label: '' }, ts: '2026-03-20T14:19:00Z' })
    const entry = resolveRenderer(m, defaultMessageRenderers)!
    const ctx = {
      row: (node: React.ReactNode) => node,
      messages: [prior, m],
      index: 1,
    } as unknown as MessageRenderContext
    const { container } = render(<>{entry.render(m, ctx)}</>)
    expect(container.querySelector('[data-testid="section-marker-row"]')).not.toBeNull()
  })
})

describe('the marker stamp across days', () => {
  // The stamp's whole job is telling two breaks apart. A bare clock does that inside one
  // day and fails across days, which is the session length this feature exists for.
  const AT = '2026-03-04T14:15:07.000Z'

  afterEach(() => {
    vi.useRealTimers()
    cleanup()
  })

  function stamp(nowIso: string): string {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(nowIso))
    const m = marker({ ts: '2026-03-04T14:15:00Z', meta: { label: 'phase-one', at: AT } })
    const entry = resolveRenderer(m, defaultMessageRenderers)!
    const ctx = { row: (node: React.ReactNode) => node, messages: [m] } as unknown as MessageRenderContext
    const { container } = render(<>{entry.render(m, ctx)}</>)
    return container.querySelector('[data-testid="section-marker-time"]')!.textContent || ''
  }

  it('carries the date once the marker is not from today', () => {
    const today = stamp('2026-03-04T23:59:00.000Z')
    const later = stamp('2026-03-06T09:00:00.000Z')
    expect(today, 'a same-day stamp should stay a bare clock').not.toMatch(/Mar/)
    expect(later, 'a marker from another day needs its date').toMatch(/Mar/)
    expect(today).not.toBe(later)
  })

  it('shows the row\'s own instant, never the requested one, so order reads true', () => {
    expect(stamp('2026-03-04T23:59:00.000Z')).not.toMatch(/:07/)
    expect(stamp('2026-03-06T09:00:00.000Z')).not.toMatch(/:07/)
  })

  it('states the year only when omitting it could mislead', () => {
    expect(stamp('2027-01-05T09:00:00.000Z')).toMatch(/2026/)
    expect(stamp('2026-03-06T09:00:00.000Z')).not.toMatch(/2026/)
  })
})

describe('the capture scenes agree on how a theme is applied', () => {
  // A frame is only evidence if it photographs the shipped theme. The marker scene set a
  // bare `data-theme`, so its light frame showed a row no user sees; the fork scene was right.
  const scene = (name: string): string =>
    readFileSync(resolve(here, `../../capture/${name}.tsx`), 'utf8')

  it('both scenes set the mode attribute the stylesheet keys on', () => {
    for (const name of ['transcript-section-markers', 'fork-failure-banner']) {
      expect(scene(name), `${name} does not set data-mode`).toMatch(/dataset\.mode\s*=/)
    }
  })

  it('both scenes set the prefixed theme value, not the bare one', () => {
    for (const name of ['transcript-section-markers', 'fork-failure-banner']) {
      const src = scene(name)
      expect(src, `${name} does not set a kiro- theme`).toMatch(/kiro-light/)
      expect(src, `${name} sets a bare data-theme`).not.toMatch(
        /setAttribute\('data-theme', theme\)/,
      )
    }
  })
})
