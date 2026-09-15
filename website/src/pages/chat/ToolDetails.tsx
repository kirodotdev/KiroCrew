/**
 * Shared inline detail panel for tool calls. Used in two places:
 *
 *   1. Inline pill ({@link ../chat/ToolCallLine}) — expanded below the pill
 *      button when the user clicks it or when the tool is awaiting approval.
 *   2. Approval bar ghost ({@link ../../components/ChatInput}) — rendered
 *      above the always-visible button row when the inline pill has scrolled
 *      out of viewport, so the user never loses sight of what the tool is
 *      about to do.
 *
 * Keeping the renderer in one place guarantees both surfaces show identical
 * details — meta row chips, segmented Input/Output toggle, preformatted
 * payload blocks — with no risk of drift.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Zap, Wrench } from 'lucide-react'
import { ToolInputText } from '../../components/ToolInputText'
import { ROW_RAIL_CLASS } from './rowPill'
import { PierreCode } from '../../pierre'
import SegmentedControl from '../../components/SegmentedControl'

/** Compact single-value rendering inside the tool-details table: no gutter,
 *  wrapped lines, transparent background so the row's own surface shows. */
const CMD_CODE_OPTIONS = { disableLineNumbers: true, overflow: 'wrap' } as const

import { i18nT } from '../../i18n/t'
export function ToolDetails({ purpose, pillLabel, toolName, input, output, auto, pending, ts, hasEntry, fmtTime, barColor, layoutId, compact, flush }: {
  purpose: string
  /** What the pill itself displays. The meta row hides the `→ purpose` line
   *  when it would just duplicate the pill text — happens when
   *  `simplifiedToolNames` is on and the pill is already showing the purpose. */
  pillLabel: string
  /** Raw tool name (e.g. the underlying tool identifier). Rendered as a chip in
   *  the meta row whenever the pill is showing something else (the purpose),
   *  so an expanded tool call always reveals which tool actually ran. Hidden
   *  when it would duplicate the pill label. */
  toolName?: string
  input: string; output: string; auto: boolean; pending: boolean; ts: number; hasEntry: boolean
  fmtTime: (t: number) => string
  /** Full CSS color value for the left rail (typically a `color-mix(...)` of
   *  a theme variable). Mirrors the pill icon's status colour so the panel
   *  visually chains off the pill. */
  barColor: string
  /** Stable per-pill id so framer-motion's segmented-control pill animates within
   *  this instance only — sharing the layoutId across pills would cause the
   *  active highlight to fly between unrelated tool calls. */
  layoutId: string
  /** When true, render in a tighter layout: single-line purpose clamp and
   *  smaller pre max-height. Used by the approval bar's ghost mirror so an
   *  unusually large input doesn't make the ghost surface dominate the
   *  screen — users can hit "Show in chat" to see the inline pill's full
   *  view if details get cut off. */
  compact?: boolean
  /** Put the status rail ON the container's left edge instead of indenting it.
   *  The transcript row wants this: its pill icon already sits on the message
   *  column's text edge, and a rail indented from that edge reads as a second,
   *  contradictory left margin in the same row. The approval bar's ghost mirror
   *  does NOT set it — that surface lives inside the composer's own box, where
   *  the indent separates the rail from the box edge. */
  flush?: boolean
}) {
  const hasInput = !!input
  const hasOutput = !!output
  // Default: prefer Output if present, else Input. Tracks user intent so we
  // don't yank focus away from a section the user explicitly opened.
  const [section, setSection] = useState<'input' | 'output'>(hasOutput ? 'output' : 'input')
  // Raw vs Formatted payload rendering. Formatted (default)
  // unescapes \n/\t so multi-line commands are legible; Raw shows the exact
  // verbatim payload for faithful pre-approval inspection. Only surfaced for
  // JSON-ish payloads, where the two modes actually differ.
  const [viewMode, setViewMode] = useState<'formatted' | 'raw'>('formatted')
  const userPickedRef = useRef(false)
  const onSectionChange = useCallback((s: 'input' | 'output') => {
    userPickedRef.current = true
    setSection(s)
  }, [])
  // Live tools: output arrives after input. Auto-promote to Output when it
  // first becomes available (unless the user has explicitly stayed on Input).
  useEffect(() => {
    if (hasOutput && !userPickedRef.current && section !== 'output') setSection('output')
  }, [hasOutput, section])

  const empty = !purpose && !hasInput && !hasOutput
  // Active section guarded against picking a disabled segment (e.g. user clicked
  // Output earlier, then re-opened a tool that hadn't received output yet).
  const active: 'input' | 'output' =
    section === 'output' && !hasOutput ? 'input' :
    section === 'input' && !hasInput ? 'output' : section
  // The Raw/Formatted toggle only matters for JSON-ish payloads (the sole place
  // the whitespace unescape applies) — hide it for plain text / diff output.
  const activeText = active === 'input' ? input : output
  const activeIsJson = /^\s*[{[]/.test(activeText)
  const rawMode = viewMode === 'raw'
  // Whether to offer a section control at all. Two sections is always a real
  // choice. ONE section only earns a naming label on the inline panel, which the
  // user reads at length; the approval bar's compact ghost stays bare, matching
  // the `compact` contract above — and there the label would say nothing anyway,
  // since a ghost only ever mirrors a pending call, which has input and no output.
  const showSection = (hasInput && hasOutput) || !compact
  // Only show the purpose line when it adds info the pill isn't already showing.
  const showPurpose = !!purpose && purpose.trim() !== pillLabel.trim()
  // Show the raw tool name when the pill is displaying something else (the
  // purpose, under simplifiedToolNames) — otherwise the expanded panel would
  // never reveal which tool actually ran. Suppressed when it duplicates the pill.
  const showToolName = !!toolName && toolName.trim() !== pillLabel.trim()
  // Recompute `empty` against the actual render predicates: the empty hint
  // should appear whenever the meta row, I/O blocks, and purpose line are all
  // suppressed. Without this, a historical tool whose only persisted meta is
  // a `purpose` that dedups against the pill label would render an empty
  // colored bar with no content (purpose is truthy → `empty` was false, but
  // `showPurpose` is false → meta row hidden, no I/O → blocks hidden).
  const reallyEmpty = (empty && !showToolName) || (!showPurpose && !showToolName && !hasInput && !hasOutput && !auto && !pending && ts === 0)

  return (
    <div className={`${flush ? '' : 'ml-3'} ${ROW_RAIL_CLASS} flex flex-col gap-2`} style={{ borderLeftColor: barColor }}>
      {(auto || pending || ts > 0 || showToolName || showPurpose || hasInput || hasOutput) && (
        <div className="flex items-center gap-2 flex-wrap">
          {/* Capped and clipped: `toolName` is kiro-cli's display TITLE, and for a
              shell call that title is the whole command line — wide enough to
              consume the row on its own. The full string stays reachable via the
              tooltip and is rendered verbatim in the payload table below. */}
          {showToolName && (
            <span
              className="inline-flex items-center gap-1 min-w-0 max-w-[min(100%,320px)] px-2 py-0.5 rounded-md border border-border bg-bg-elevated text-text text-[11px] leading-4 font-mono"
              title={toolName}
            >
              <Wrench size={10} className="text-muted shrink-0" /> <span className="truncate">{toolName}</span>
            </span>
          )}
          {ts > 0 && <span className="inline-flex items-center px-2 py-0.5 rounded-md border border-border bg-bg-elevated text-muted text-[11px] leading-4 font-mono">{fmtTime(ts)}</span>}
          {pending && (
            <span
              className="inline-flex items-center px-2 py-0.5 rounded-md text-[11px] leading-4 font-mono"
              style={{
                color: 'var(--warn)',
                backgroundColor: 'color-mix(in srgb, var(--warn) 8%, transparent)',
                border: '1px solid color-mix(in srgb, var(--warn) 30%, transparent)',
              }}
            >
              {i18nT('pages.chat.toolDetails.waiting_for_approval')}
            </span>
          )}
          {auto && <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-border bg-bg-elevated text-muted text-[11px] leading-4 font-mono"><Zap size={10} /> {i18nT('pages.chat.toolDetails.auto')}</span>}
          {showPurpose && <span className={`text-[12px] leading-5 text-muted/50 break-words min-w-0 ${compact ? 'line-clamp-1' : ''}`}>→ {purpose}</span>}
        </div>
      )}
      {(hasInput || hasOutput) && (
        <>
          {(showSection || activeIsJson) && (
          /* A bare row of its own, directly above the payload it controls. In the
              meta row these needed `ml-auto` to reach the right edge, which once
              the chips filled the line stranded them right-aligned on an empty
              wrapped row. Nothing else lives here, so `justify-between` is honest:
              section on the left, render mode on the right. No frame — the payload
              box below already carries one, and a second around this pair reads as
              boxes inside boxes.

              `flex-wrap` matters: the capsules are `inline-flex` with the default
              `min-width: auto`, so they cannot shrink, and the transcript row's
              disclosure wrapper clips overflow — without wrapping, the render-mode
              toggle is cut off in the composer's compact mirror and on a phone.
              Wrapping here cannot recreate the stranding it replaced: a wrapped
              line holds one item, and `justify-between` leaves a lone item at the
              start rather than pushing it to the right. */
          <div className="flex items-center justify-between gap-2 flex-wrap">
            {showSection && (
              <ToolSegmented
                active={active}
                hasInput={hasInput}
                hasOutput={hasOutput}
                onChange={onSectionChange}
                layoutId={layoutId}
              />
            )}
            {activeIsJson && (
              <ViewModeToggle mode={viewMode} onChange={setViewMode} layoutId={`${layoutId}-view`} />
            )}
          </div>
          )}
          <AnimatePresence mode="wait" initial={false}>
            {active === 'input' && hasInput && (
              <motion.div
                key="input"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.18, ease: [0.4, 0, 0.2, 1] }}
                className="overflow-hidden"
              >
                <PayloadView text={input} raw={rawMode} maxH={compact ? 'max-h-[160px]' : 'max-h-[400px]'} />
              </motion.div>
            )}
            {active === 'output' && hasOutput && (
              <motion.div
                key="output"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.18, ease: [0.4, 0, 0.2, 1] }}
                className="overflow-hidden"
              >
                <PayloadView text={output} raw={rawMode} maxH={compact ? 'max-h-[160px]' : 'max-h-[500px]'} />
              </motion.div>
            )}
          </AnimatePresence>
        </>
      )}
      {reallyEmpty && (
        <div className="text-[12px] leading-5 text-muted/60 italic">
          {hasEntry ? i18nT('pages.chat.toolDetails.no_input_or_output_captured_for_this_tool_call') : i18nT('pages.chat.toolDetails.details_unavailable_for_historical_tool_calls')}
        </div>
      )}
    </div>
  )
}

/** Catalog key per payload section — the same resolvable shape as
 *  `FILTER_LABEL_KEY` in ChatSidebar. Reuses the Activity panel's own
 *  Input/Output keys rather than minting a second pair: identical words for the
 *  same two halves of a tool call, and a duplicate pair is translation debt in
 *  eleven catalogs for no gain. */
const SECTION_LABEL_KEY: Record<'input' | 'output', string> = {
  input: 'pages.chat.activityViewer.input',
  output: 'pages.chat.activityViewer.output',
}

/** Section switch for the payload below.
 *
 *  `collapse={false}` is required and is the escape hatch `SegmentedControl`
 *  documents for a parent that hugs its content: the responsive full → compact →
 *  dropdown measurement reads the PARENT's width, so in a content-sized row it
 *  measures this control itself and the answer is circular.
 *
 *  With only one section available there is nothing to switch between, so the
 *  control degrades to a plain label. A live tool spends its whole pre-approval
 *  life in that state, and a two-segment capsule with one segment greyed out
 *  advertises a choice the user does not have. The label keeps the one thing
 *  that capsule was still carrying: which section is on screen. */
function ToolSegmented({ active, hasInput, hasOutput, onChange, layoutId }: {
  active: 'input' | 'output'
  hasInput: boolean
  hasOutput: boolean
  onChange: (s: 'input' | 'output') => void
  layoutId: string
}) {
  if (!hasInput || !hasOutput) {
    return (
      <span className="text-[11px] leading-4 font-mono text-muted px-1 py-0.5 tracking-wide">
        {i18nT(SECTION_LABEL_KEY[active])}
      </span>
    )
  }
  return (
    <SegmentedControl<'input' | 'output'>
      segments={[
        { key: 'input', label: i18nT(SECTION_LABEL_KEY.input) },
        { key: 'output', label: i18nT(SECTION_LABEL_KEY.output) },
      ]}
      value={active}
      onChange={onChange}
      layoutId={layoutId}
      collapse={false}
    />
  )
}

/** Formatted vs Raw payload rendering. Only shown for JSON-ish payloads, where
 *  the two modes actually differ. Same `collapse={false}` reasoning as
 *  {@link ToolSegmented}. */
function ViewModeToggle({ mode, onChange, layoutId }: {
  mode: 'formatted' | 'raw'
  onChange: (m: 'formatted' | 'raw') => void
  layoutId: string
}) {
  return (
    <SegmentedControl<'formatted' | 'raw'>
      segments={[
        {
          key: 'formatted',
          label: i18nT('pages.chat.toolDetails.formatted'),
          tooltip: i18nT('pages.chat.toolDetails.render_escaped_whitespace_as_real_line_breaks'),
        },
        {
          key: 'raw',
          label: i18nT('pages.chat.toolDetails.raw'),
          tooltip: i18nT('pages.chat.toolDetails.show_the_exact_payload_escaping_preserved'),
        },
      ]}
      value={mode}
      onChange={onChange}
      layoutId={layoutId}
      collapse={false}
    />
  )
}


/** Parse text as a JSON object for the Formatted table view. Returns null for
 *  non-objects, arrays, or unparseable/streaming payloads so the caller can
 *  fall back to the highlighted-text renderer. */
function tryParseJsonObject(text: string): Record<string, unknown> | null {
  if (!/^\s*\{/.test(text)) return null
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    return null
  }
}

/** Render a single parsed JSON value. Strings decode escapes natively via
 *  JSON.parse, so a multi-line command shows real quotes + line breaks in a
 *  <pre> cell; scalars render inline with type coloring; nested objects/arrays
 *  fall back to indented, highlighted JSON. */
function JsonValue({ value, lang }: { value: unknown; lang?: string }): ReactNode {
  if (value === null) return <span style={{ color: 'var(--json-bool)' }}>{i18nT('pages.chat.toolDetails.null')}</span>
  if (typeof value === 'boolean') return <span style={{ color: 'var(--json-bool)' }}>{String(value)}</span>
  if (typeof value === 'number') return <span style={{ color: 'var(--json-num)' }}>{value}</span>
  if (typeof value === 'string') {
    // Known command-bearing keys get Pierre-based syntax highlighting.
    if (lang) {
      return (
        <div className="pierre-surface pierre-transparent">
          <PierreCode
            file={{ name: `value.${lang}`, contents: value }}
            langHint={lang}
            options={CMD_CODE_OPTIONS}
          />
        </div>
      )
    }
    if (value.includes('\n')) {
      return (
        <pre className="m-0 whitespace-pre-wrap break-all font-mono" style={{ color: 'var(--json-str)' }}>{value}</pre>
      )
    }
    return <span className="break-all" style={{ color: 'var(--json-str)' }}>{value}</span>
  }
  return (
    <pre className="m-0 whitespace-pre-wrap break-all font-mono">
      <ToolInputText text={JSON.stringify(value, null, 2)} />
    </pre>
  )
}

/** Formatted table view of a parsed JSON object: one row per top-level key,
 *  key in the left column, {@link JsonValue}-rendered value on the right. */
function JsonTable({ data }: { data: Record<string, unknown> }): ReactNode {
  return (
    <table className="w-full border-collapse text-[12px] font-mono">
      <tbody>
        {Object.entries(data).map(([k, v]) => {
          // Shell-command keys render their string value with bash highlighting.
          const lang = /^(command|cmd|script|shell|bash)$/i.test(k) ? 'bash' : undefined
          return (
            <tr key={k} className="align-top border-b border-border/40 last:border-b-0">
              <td className="py-1 pr-3 align-top whitespace-nowrap" style={{ color: 'var(--json-key)' }}>{k}</td>
              <td className="py-1 align-top"><JsonValue value={v} lang={lang} /></td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

/** Payload block for the Input/Output panes. Formatted mode renders a parsed
 *  JSON object as a {@link JsonTable}; Raw mode (and any unparseable payload)
 *  falls back to the verbatim/highlighted {@link ToolInputText} in a <pre>.
 *
 *  This box is the panel's ONLY frame — the controls above it are a bare row, so
 *  nothing here nests a border inside a border. `px-2` matches the meta-row
 *  chips' own padding, giving the chip row and the table one shared inner inset
 *  rather than a 2px step between them. */
function PayloadView({ text, raw, maxH }: { text: string; raw: boolean; maxH: string }): ReactNode {
  const base = `px-2 py-2 bg-bg-elevated rounded-md text-[12px] font-mono ${maxH} overflow-y-auto leading-5 border border-border`
  if (!raw) {
    const parsed = tryParseJsonObject(text)
    if (parsed) {
      return <div className={`${base} overflow-x-auto`}><JsonTable data={parsed} /></div>
    }
  }
  return <pre className={`${base} whitespace-pre-wrap break-all`}><ToolInputText text={text} raw={raw} /></pre>
}
