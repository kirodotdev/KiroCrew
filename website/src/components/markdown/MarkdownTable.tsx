import React, { useEffect, useRef, useState } from 'react'
import { Check, Copy, FileSpreadsheet } from 'lucide-react'
import type { Element as HastElement } from 'hast'
import { copyToClipboard } from '../../utils/clipboard'
import { hastTableToCsv, hastTableToMarkdown } from '../../utils/tableClipboard'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../../utils/touchActions'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'
import { sp } from './elements'

const TABLE_ACTION_BTN_CLS = 'flex items-center gap-1 px-1.5 py-1 rounded text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer'

/** A markdown table plus the row of copy actions beneath it.
 *
 *  Selecting a rendered table by hand and pasting it produces tab-separated
 *  cells at best and a run of words at worst, so the copy has to be offered.
 *  Two targets, because they are pasted into different places: GFM Markdown
 *  for a doc, an issue, or another chat, and CSV for a spreadsheet. Both are
 *  serialized from the hast `node` react-markdown hands this override, never
 *  from the DOM -- see `tableClipboard.ts` for why (alignment is not forwarded
 *  to the DOM, and inline-code chips carry UI a text walk cannot tell apart
 *  from content).
 *
 *  The row follows the code block's pattern exactly: hidden until the table is
 *  hovered or focused (`group-hover` / `group-focus-within`), and always shown
 *  on a hover-less (touch) device through `HOVER_NONE_ACTIONS_ROW_CLS`, so it
 *  is discoverable there without adding permanent chrome under every table on
 *  a desktop. It sits BELOW the table, not over the header cells, so it never
 *  covers a column label. Each button carries a short visible verb label
 *  beside its glyph ("Copy Markdown", "Copy CSV") -- a touch screen shows no
 *  tooltip, so the word alone must say what a tap does; it flips to "Copied!"
 *  on success so the confirmation reads as text, not only as a colour.
 *
 *  The horizontal-scroll wrapper and the table's own class contract are
 *  unchanged (`MarkdownRenderer.tableWrap.test.tsx` pins them): the wrapper
 *  still owns `overflow-x-auto`, and this component only adds a sibling row
 *  after it. */
export function MarkdownTable({ node, children }: { node?: HastElement; children?: React.ReactNode }) {
  type CopyTarget = 'markdown' | 'csv'
  type CopyOutcome = { state: 'idle' } | { state: 'ok'; target: CopyTarget } | { state: 'failed' }
  const [outcome, setOutcome] = useState<CopyOutcome>({ state: 'idle' })
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timerRef.current != null) clearTimeout(timerRef.current) }, [])

  const copy = (target: CopyTarget) => {
    if (!node) return
    const text = target === 'markdown' ? hastTableToMarkdown(node) : hastTableToCsv(node)
    if (text.length === 0) return
    copyToClipboard(text).then(
      ok => {
        if (!ok) { setOutcome({ state: 'failed' }); return }
        setOutcome({ state: 'ok', target })
        if (timerRef.current != null) clearTimeout(timerRef.current)
        timerRef.current = setTimeout(() => { setOutcome({ state: 'idle' }); timerRef.current = null }, 1500)
      },
      () => setOutcome({ state: 'failed' }),
    )
  }

  const label = (target: CopyTarget) => outcome.state === 'ok' && outcome.target === target
    ? i18nT('components.markdownRenderer.copied')
    : target === 'markdown'
      ? i18nT('components.markdownRenderer.copy_table_markdown')
      : i18nT('components.markdownRenderer.copy_table_csv')
  // The visible word carries the verb ("Copy Markdown"), because on a touch
  // screen it is the only label there is, and it flips to "Copied!" with the
  // check so the confirmation is readable, not just a colour change.
  const word = (target: CopyTarget) => outcome.state === 'ok' && outcome.target === target
    ? i18nT('components.markdownRenderer.copied')
    : target === 'markdown'
      ? i18nT('components.markdownRenderer.format_markdown')
      : i18nT('components.markdownRenderer.format_csv')
  const glyph = (target: CopyTarget, Icon: typeof Copy) => outcome.state === 'ok' && outcome.target === target
    ? <Check size={13} className="text-ok" aria-hidden="true" />
    : <Icon size={13} aria-hidden="true" />

  return (
    <div className="my-3 group/table" data-testid="markdown-table">
      <div className="overflow-x-auto"><table {...sp(node)} className="min-w-full border-collapse text-sm [overflow-wrap:normal] [word-break:normal]">{children}</table></div>
      <div className={`mt-0.5 flex items-center justify-end gap-1 select-none opacity-0 group-hover/table:opacity-100 group-focus-within/table:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
        <button type="button" data-testid="table-copy-markdown" className={TABLE_ACTION_BTN_CLS} onClick={() => copy('markdown')} title={label('markdown')} aria-label={label('markdown')}>
          {glyph('markdown', Copy)}
          <span aria-hidden="true">{word('markdown')}</span>
        </button>
        <button type="button" data-testid="table-copy-csv" className={TABLE_ACTION_BTN_CLS} onClick={() => copy('csv')} title={label('csv')} aria-label={label('csv')}>
          {glyph('csv', FileSpreadsheet)}
          <span aria-hidden="true">{word('csv')}</span>
        </button>
      </div>
      {/* No hand-off, for the same reason as `MermaidBlock`'s notices: this
          renderer is embedded in hosts holding unsaved drafts it cannot
          identify -- MarkdownPanel's editable preview, the chat composer -- so
          navigating to the chat could discard what the user typed. Dismissable,
          like the mermaid copy notice: one refused clipboard write must not
          leave a permanent red line under the table in the transcript. */}
      {outcome.state === 'failed' && (
        <ErrorNotice variant="inline" className="mt-1" message={i18nT('components.markdownRenderer.copy_failed')} onDismiss={() => setOutcome({ state: 'idle' })} />
      )}
    </div>
  )
}
