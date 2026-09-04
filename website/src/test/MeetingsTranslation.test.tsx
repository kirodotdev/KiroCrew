// The live-translation side panel and the wiring that feeds it.
//
// The panel is rendered directly (its props are pure data), and the parts that
// live inside the session hook — incremental cursor accumulation, and the gate
// that stops polling for a feature nobody enabled — are pinned against the
// shipping source, the technique MeetingsSessionLogic.test.ts established for
// hook internals that cannot be rendered in isolation.

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import TranslationSidebar from '../apps/meetings/components/TranslationSidebar'
import type { TranslationLine } from '../apps/meetings/api'
import EN_CATALOG from '../i18n/locales/en.json'

const SessionSource = readFileSync('src/apps/meetings/hooks/useMeetingSession.ts', 'utf-8')
const ViewSource = readFileSync('src/apps/meetings/MeetingView.tsx', 'utf-8')
const ApiSource = readFileSync('src/apps/meetings/api.ts', 'utf-8')

const line = (n: number, source: string, text: string): TranslationLine => ({ n, source, text })

const renderPanel = (over: Partial<Parameters<typeof TranslationSidebar>[0]> = {}) =>
  render(
    <TranslationSidebar
      lines={[]}
      languageLabel="日本語"
      pending={0}
      dropped={0}
      loading={false}
      onClose={() => {}}
      {...over}
    />,
  )

describe('TranslationSidebar', () => {
  it('shows the target language as its own endonym', () => {
    // Not translated on purpose: a reader looking for Japanese recognises 日本語.
    renderPanel()
    expect(screen.getByText('日本語')).toBeTruthy()
  })

  it('shows the source line beside its translation', () => {
    // Both halves, because the panel exists for someone who only partly follows the
    // meeting — seeing them together is what lets them check a doubtful translation
    // against what was actually said.
    renderPanel({ lines: [line(0, 'we ship on Friday', '金曜日にリリースします')] })
    expect(screen.getByText('we ship on Friday')).toBeTruthy()
    expect(screen.getByText('金曜日にリリースします')).toBeTruthy()
  })

  it('marks a failed line instead of dropping it', () => {
    // An empty translation is persisted precisely so the line is not a silent gap
    // the user cannot tell apart from "nobody spoke".
    renderPanel({ lines: [line(0, 'we ship on Friday', '')] })
    expect(screen.getByText('we ship on Friday')).toBeTruthy()
    expect(
      screen.getByText(EN_CATALOG.apps.meetings.translation.lineFailed),
    ).toBeTruthy()
  })

  it('renders lines in spoken order', () => {
    renderPanel({
      lines: [line(0, 'first', 'un'), line(1, 'second', 'deux'), line(2, 'third', 'trois')],
    })
    const body = document.body.textContent ?? ''
    expect(body.indexOf('un')).toBeLessThan(body.indexOf('deux'))
    expect(body.indexOf('deux')).toBeLessThan(body.indexOf('trois'))
  })

  it('explains an empty panel differently while loading', () => {
    const idle = renderPanel({ loading: false })
    expect(
      idle.getByText(EN_CATALOG.apps.meetings.translation.emptyHint),
    ).toBeTruthy()
    idle.unmount()

    const busy = renderPanel({ loading: true })
    expect(busy.getByText(EN_CATALOG.apps.meetings.translation.loading)).toBeTruthy()
  })

  it('says it is catching up rather than looking stuck', () => {
    // Translation runs one line at a time behind live speech, so a backlog is normal.
    renderPanel({ lines: [line(0, 'a', 'b')], pending: 7 })
    expect(screen.getByText(EN_CATALOG.apps.meetings.translation.pending)).toBeTruthy()
  })

  it('reports dropped lines, because that is data loss', () => {
    renderPanel({ lines: [line(0, 'a', 'b')], dropped: 3 })
    expect(screen.getByText(EN_CATALOG.apps.meetings.translation.dropped)).toBeTruthy()
  })

  it('hides the status footer when there is nothing to report', () => {
    renderPanel({ lines: [line(0, 'a', 'b')] })
    expect(screen.queryByText(EN_CATALOG.apps.meetings.translation.pending)).toBeNull()
    expect(screen.queryByText(EN_CATALOG.apps.meetings.translation.dropped)).toBeNull()
  })
})

describe('the incremental poll', () => {
  it('sends a cursor rather than refetching the whole document', () => {
    // A long meeting accumulates hundreds of lines and the panel polls while open;
    // resending all of them every few seconds would grow linearly for no gain.
    expect(ApiSource).toContain('translations: (id: string, since = 0)')
    expect(ApiSource).toContain('/translations?since=')
    expect(SessionSource).toContain(
      'meetingsApi.translations(meetingId, translationCursorRef.current)',
    )
    expect(SessionSource).toContain('translationCursorRef.current = page.next_n')
  })

  it('accumulates into a Map keyed by line number, not an array', () => {
    // `queryFn` appending would duplicate every line if it ran twice for one cursor,
    // which React Strict Mode's double-invoke does in development. Keying by `n`
    // makes the merge idempotent.
    expect(SessionSource).toContain('new Map<number, TranslationLine>()')
    expect(SessionSource).toContain('translationLinesRef.current.set(line.n, line)')
  })

  it('resets when the target language changes', () => {
    // The backend starts a fresh document, so keeping the old lines would show a mix
    // with no way to tell which line is in which language.
    expect(SessionSource).toContain('lastTranslationLanguageRef')
    // The reset is keyed on the last OBSERVED server language, not the config
    // value: config moves immediately on a Settings change while the running
    // session keeps its start-time language, and comparing against config would
    // wipe the accumulator on every poll for the rest of the meeting.
    expect(SessionSource).toMatch(/if \(page\.language !== lastServerLanguageRef\.current\)/)
    // The replaced document's numbering restarted at zero, so the cursor must
    // restart with it — otherwise the new language's initial lines fall below
    // `since` and never render — and the page fetched with the stale cursor is
    // replaced by a fetch from zero rather than merged.
    const tail = SessionSource.slice(SessionSource.indexOf('lastServerLanguageRef.current = page.language'))
    const resetBlock = tail.slice(0, tail.indexOf('for (const line of page.lines)'))
    expect(resetBlock).toContain('translationLinesRef.current = new Map()')
    expect(resetBlock).toContain('translationCursorRef.current = 0')
    expect(resetBlock).toContain('page = await meetingsApi.translations(meetingId, 0)')
  })

  it('polls only while the panel is open AND a language is set', () => {
    // Translation is off by default; polling for it regardless would be pure waste.
    const enabled = SessionSource.match(/enabled: initQuery\.isSuccess && [^\n]*/)
    expect(enabled).toBeTruthy()
    expect(enabled![0]).toContain('translationOpen')
    expect(enabled![0]).toContain('Boolean(translationLanguage)')
  })

  it('keeps polling at the idle rate while paused or reviewing', () => {
    // Pausing does not clear the backend queue — the worker keeps draining and
    // persisting lines — so stopping the poll entirely would freeze the panel
    // mid-sentence and never render the tail. Same ladder as the sibling
    // outputs/transcript queries.
    const tail = SessionSource.slice(SessionSource.indexOf('const translationQuery'))
    const ladder = tail.match(/refetchInterval:[\s\S]*?\n  \}\)/)
    expect(ladder).toBeTruthy()
    expect(ladder![0]).toContain("status === 'paused' || status === 'reviewing'")
    expect(ladder![0]).toContain('poll_interval_idle')
  })
})

describe('MeetingView wiring', () => {
  it('offers the toggle only when a language is configured', () => {
    // With translation off the item would open a panel that can never fill.
    expect(ViewSource).toMatch(/\{translation\.language && \(\s*<DropdownMenuItem/)
  })

  it('keeps the toolbar within the two-control cap via an overflow menu', () => {
    // Five sibling buttons (pause/resume, end-and-review, refresh, translation,
    // tasks) breached `max-two-buttons-per-row` and wrapped under width pressure.
    // The row keeps the one primary status action; everything else lives in a
    // DropdownMenu, whose trigger counts as one control.
    expect(ViewSource).toContain('<DropdownMenuTrigger asChild>')
    // The secondary actions are menu items (onSelect) now, not sibling buttons
    // (onClick) in the row.
    const moved: [string, string][] = [
      ['onSelect={session.refresh}', 'onClick={session.refresh}'],
      ['session.setTranslationOpen(open => !open)', 'onClick={() => session.setTranslationOpen'],
      ['setSidebarOpen(open => !open)', 'onClick={() => setSidebarOpen'],
      ['onSelect={actions.review}', 'onClick={actions.review}'],
    ]
    for (const [inMenu, asButton] of moved) {
      expect(ViewSource, `${inMenu} must live in the overflow menu`).toContain(inMenu)
      expect(ViewSource, `${asButton} must not remain a row button`).not.toContain(asButton)
    }
  })

  it('keeps the two side panels mutually exclusive', () => {
    // Stacked below `lg`, both panels' 260px height floors together exceed a
    // short viewport (2 × min-h-[260px] inside an overflow-hidden column) and
    // squeeze the transcript out entirely — so opening one closes the other.
    expect(ViewSource).toMatch(/setSidebarOpen\(false\)\s*session\.setTranslationOpen\(open => !open\)/)
    expect(ViewSource).toMatch(/session\.setTranslationOpen\(false\)\s*setSidebarOpen\(open => !open\)/)
  })

  it('mounts the panel only when open and configured', () => {
    expect(ViewSource).toContain('{translation.open && translation.language && (')
  })

  it('takes the language label from the server, not a client-side list', () => {
    // The backend publishes the accepted languages and their endonyms, so a second
    // copy in the client would be the thing that drifts.
    expect(ViewSource).toContain('languageLabel={translation.languageLabel}')
    expect(SessionSource).toContain('page.language_label')
  })

  it('releases the sidebar width when narrow, like TaskSidebar', () => {
    // A fixed 340px column beside the meeting clips inside a 320px viewport; the
    // panel stacks with a bounded height below `lg`, the shape TaskSidebar and
    // appSplitsNarrowA already pin for this app.
    const { container } = renderPanel()
    const aside = container.querySelector('aside')!
    expect(aside.className).toMatch(/\bw-full\b/)
    expect(aside.className).toMatch(/lg:w-\[340px\]/)
    expect(aside.className).toMatch(/h-\[42%\]/)
    expect(aside.className).toMatch(/min-h-\[260px\]/)
    expect(aside.className).toMatch(/lg:h-full/)
    // The divider turns with the layout.
    expect(aside.className).toMatch(/border-t border-border/)
    expect(aside.className).toMatch(/lg:border-t-0 lg:border-l/)
    expect(aside.className).not.toMatch(/^flex-none w-\[340px\]/)
  })
})
