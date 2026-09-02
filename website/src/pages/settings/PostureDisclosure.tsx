import { useMemo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronRight, AlertTriangle, ExternalLink } from 'lucide-react'
import { Badge, SearchInput } from '../../components/ui'
import Clickable from '../../components/Clickable'
import type { PostureControl, PostureItem } from '../../api/client'

import { i18nT } from '../../i18n/t'
/* ── Expandable security-posture row ──
 *
 * Each posture count is a disclosure rather than a dead pill: click it and the
 * concrete list it covers expands inline, filterable once it gets long.
 *
 * Counts come from the server as `items.length`, so the pill and the list can
 * never disagree. A `count` of null means the control was temporarily
 * unresolvable — rendered as an explicit warning, never as "0", so an operator
 * is never told a control covers nothing when it may well be active.
 */

/** Base for source deep links. Exported so `SecurityPanel` shares this one
 *  definition rather than each file carrying its own copy of the repo URL. */
export const CODE_BASE = 'https://github.com/kirodotdev/KiroCrew/blob/main'

/** Show the filter box once scanning the list by eye stops being practical. */
const FILTER_THRESHOLD = 12

/** Cap the initial render of a very long list; the rest is one click away.
 *  Keeps the 137-rule deny list from pushing 137 rows into the DOM on expand. */
const INITIAL_VISIBLE = 25

/** Heuristic: render an item label as code only when it looks like code.
 *
 *  Labels are heterogeneous by design — some are identifiers (`~/.aws`,
 *  `spawn_run`, `| bash`, `dashboard/state.py`) and some are English (a deny
 *  rule's description, "AWS access keys", "Slack handler"). Mono + `break-all`
 *  is right for the former and wrong for the latter (it hyphenates mid-word).
 *
 *  Requires a code SHAPE, not merely brevity: a length-only rule
 *  ("short ⇒ code") mis-fonts short *English phrases*, which are not
 *  identifiers. So: no spaces at all, or a path/glob/shell/extension marker. */
function isCodeLabel(label: string): boolean {
  return !label.includes(' ') || /[~/|*$\\]|^-|\.\w+$/.test(label)
}

function ItemRow({ item }: { item: PostureItem }) {
  const code = isCodeLabel(item.label)
  return (
    <div className="py-1.5 pl-6 pr-1">
      <div
        className={
          code
            ? 'text-[13px] text-text font-mono break-all'
            : 'text-[13px] text-text font-body leading-relaxed break-words'
        }
      >
        {item.label}
      </div>
      {item.detail && (
        <div className="text-[12px] text-muted mt-0.5 leading-relaxed break-words font-body">{item.detail}</div>
      )}
    </div>
  )
}

export function PostureDisclosureRow({
  control,
  icon,
  /** Overrides the server count — used by the denied-commands row, whose live
   *  effective count (after opt-outs + policy pins) differs from the shipped
   *  rule-table length the registry reports. */
  countOverride,
  /** Extra note rendered under the summary, e.g. "N of M enabled". */
  note,
}: {
  control: PostureControl
  icon: React.ReactNode
  countOverride?: number | null
  note?: string
}) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  // Which filter query the user asked to see in full (null = none).
  const [expandedFor, setExpandedFor] = useState<string | null>(null)

  const count = countOverride !== undefined ? countOverride : control.count
  // Keyed off the RESOLVED count, not `control.unavailable`: when a caller supplies
  // a live override the row has a real number to show, so the server's degraded
  // flag must not override it into a warning. A null count is the only honest
  // "unresolved" — and it is never rendered as 0, which would read as "this
  // control covers nothing".
  const unresolved = count === null

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return control.items
    return control.items.filter(
      i => i.label.toLowerCase().includes(q) || i.detail.toLowerCase().includes(q)
    )
  }, [control.items, filter])

  // "Show all" is scoped to the filter it was granted under. Keying it to the
  // query (not a bare boolean) resets the INITIAL_VISIBLE DOM cap whenever the
  // set changes, so an expansion of a NARROW filtered subset cannot survive
  // clearing that filter and dump every row — while still letting the user
  // reach the tail of a filtered list (which a plain `expanded && !filter`
  // guard would make unreachable).
  const query = filter.trim()
  const showAll = expandedFor !== null && expandedFor === query
  const visible = showAll ? filtered : filtered.slice(0, INITIAL_VISIBLE)
  const hidden = filtered.length - visible.length
  // An expandable row needs something to expand INTO. A resolved control with no
  // items (possible for a future control whose list is genuinely empty) stays a
  // plain row rather than an affordance that opens onto nothing.
  const canExpand = control.items.length > 0

  // The badge carries the row's actual payload, so fold it into the accessible
  // name — an `aria-label` overrides name-from-content, and a keyboard user
  // hearing only "Show Output redaction details" would miss the count entirely.
  const headerLabel = canExpand
    ? `${open ? 'Hide' : 'Show'} ${control.label} details — ${
        unresolved ? 'currently unavailable' : `${count} ${control.unit}`
      }`
    : undefined

  const headerInner = (
    <>
      <div className="flex items-center gap-2.5 min-w-0">
        {canExpand ? (
          <motion.span
            className="shrink-0 text-muted group-hover:text-text transition-colors flex items-center"
            animate={{ rotate: open ? 90 : 0 }}
            transition={{ type: 'spring', damping: 22, stiffness: 300 }}
          >
            <ChevronRight className="lucide-inline" />
          </motion.span>
        ) : (
          <span className="shrink-0 w-3.5" aria-hidden="true" />
        )}
        <span className="text-muted shrink-0 flex items-center">{icon}</span>
        <span className="text-[13px] font-semibold text-text group-hover:text-text-strong transition-colors truncate">
          {control.label}
        </span>
      </div>
      <div className="flex items-center gap-1.5 shrink-0">
        {unresolved ? (
          <Badge variant="warn"><AlertTriangle className="lucide-inline" /> {i18nT('pages.settings.postureDisclosure.unavailable')}</Badge>
        ) : (
          <Badge variant="ok" className="tabular-nums">{count} {control.unit}</Badge>
        )}
      </div>
    </>
  )

  const headerClass = `flex items-center justify-between gap-3 py-2.5 group rounded-md transition-colors ${canExpand ? 'cursor-pointer hover:bg-bg-hover/40' : 'cursor-default'}`

  return (
    <div className="border-t border-border first:border-t-0">
      {/* A control with nothing to expand renders as a PLAIN row, not a disabled
          Clickable: `disabled` would emit role="button" + aria-disabled, so an
          unavailable control would announce as a broken button instead of the
          status row it actually is (matching StatusRow's plain-div precedent). */}
      {canExpand ? (
        <Clickable
          onClick={() => setOpen(o => !o)}
          aria-expanded={open}
          aria-label={headerLabel}
          className={headerClass}
        >
          {headerInner}
        </Clickable>
      ) : (
        <div className={headerClass}>{headerInner}</div>
      )}
      <AnimatePresence initial={false}>
        {open && canExpand && (
          <motion.div
            key="detail"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.7 }}
            style={{ overflow: 'hidden' }}
          >
            <div className="pb-2 pl-6">
              {control.summary && (
                <div className="text-[12px] text-muted leading-relaxed pr-2 pb-1.5">{control.summary}</div>
              )}
              {note && <div className="text-[12px] text-muted leading-relaxed pr-2 pb-1.5">{note}</div>}

              {control.items.length >= FILTER_THRESHOLD && (
                <div className="pb-1.5 pr-2">
                  <SearchInput
                    value={filter}
                    onChange={e => setFilter(e.target.value)}
                    placeholder={i18nT('pages.settings.postureDisclosure.filter_items', { n: control.items.length, unit: control.unit })}
                    aria-label={i18nT('pages.settings.postureDisclosure.filter', { label: control.label })}
                  />
                </div>
              )}

              {filtered.length === 0 ? (
                <div className="text-[12px] text-muted py-2 pl-6">{i18nT('pages.settings.postureDisclosure.no_matches_for_query', { query: filter })}</div>
              ) : (
                <div className="divide-y divide-border rounded-md bg-bg-elevated/40">
                  {/* Index-keyed: labels are unique across every shipped control
                      today, but a future duplicate must not trip React's
                      duplicate-key path — and the list is only ever re-rendered
                      wholesale (filter/expand), so index keys cost nothing. */}
                  {visible.map((item, i) => <ItemRow key={`${control.key}-${i}`} item={item} />)}
                </div>
              )}

              {hidden > 0 && (
                <button
                  type="button"
                  className="text-[12px] text-accent hover:underline bg-transparent border-none cursor-pointer p-0 mt-1.5 ml-6"
                  onClick={() => setExpandedFor(query)}
                >
                  {i18nT('pages.settings.postureDisclosure.show')} {hidden} {i18nT('pages.settings.postureDisclosure.more')}
                </button>
              )}

              {control.source && (
                <a
                  href={`${CODE_BASE}/${control.source}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-[12px] text-accent hover:underline mt-2 ml-6 no-underline"
                >
                  <ExternalLink className="lucide-inline" />
                  <span className="font-mono">{control.source}</span>
                </a>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
