// The inline Focus Report view for the Code Review Sage app.
//
// Today a finished run's report is only reachable as an external artifact page;
// this component renders it INSIDE the app. It mirrors the semantics of
// sage_lib/report.py (bands red → yellow → green, each row's stored `why`
// rationale, the design chain, and per-finding detail) while reusing Issue
// Radar's visual language (the `Section` header treatment, pill/badge classes,
// the `rounded-lg border border-border bg-card` card shell, and markdown via
// the shared MarkdownRenderer).
//
// Security note: every model-authored prose field is rendered through
// MarkdownRenderer (which sanitizes), the outbound PR link is validated with
// safeHttpUrl, and code `snippet`s render verbatim in a <pre> (never markdown).
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import {
  ChevronRight, ClipboardCheck, ExternalLink, Loader2, MessageSquarePlus, MoreHorizontal, Share2, ShieldCheck, Wrench,
} from 'lucide-react'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../../../components/ui/dropdown-menu'
import { Btn } from '../../../components/ui'
import { safeHttpUrl } from '../../../lib/safeUrl'
import FindingCard from './FindingCard'
import ShipSummaryCard, { SHIP_KEY } from './ShipSummaryCard'
import BandChips from './BandChips'
import type { Band, ReportRow, ReviewFixFindingSnapshot, RunReport } from '../lib/types'

import { fmtDateTime } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
/** Sort order for the three bands — the report leads with what needs review. */
const BAND_RANK: Record<Band, number> = { red: 0, yellow: 1, green: 2 }

/** Per-band dot colour for the row marker. */
const BAND_DOT: Record<Band, string> = {
  red: 'text-danger',
  yellow: 'text-warn',
  green: 'text-ok',
}

/** A neutral, uppercase, tracked section label with a bottom divider — the
 * treatment lifted from Issue Radar's PrDetail `Section`. */
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10.5px] font-semibold uppercase tracking-wider text-muted mb-1">
      {children}
    </div>
  )
}

/** A small neutral pill (design risk / blast radius). */
function Pill({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full bg-bg-elevated text-muted border border-border px-2 py-0.5 text-[11px] font-medium whitespace-nowrap">
      {children}
    </span>
  )
}

function numberOrNull(value: number | string | undefined): number | null {
  if (value === undefined || value === '') return null
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function fixSnapshot(row: ReportRow, finding: NonNullable<ReportRow['findings']>[number], index: number): ReviewFixFindingSnapshot {
  const body = [finding.observation, finding.consequence]
    .filter((value): value is string => typeof value === 'string' && value.trim().length > 0)
    .join('\n\n')
  return {
    key: `${row.change_id}:finding:${index}`,
    title: finding.headline || finding.dimension || row.title,
    severity: finding.severity || row.band,
    body,
    file_path: finding.file || '',
    line: numberOrNull(finding.line),
    end_line: numberOrNull(finding.end_line),
    fingerprint: finding.fingerprint || '',
    suggested_fix: finding.suggestion || '',
  }
}

/** Derive the `#<number>` label for a PR/MR from its URL, falling back to the
 * change_id when no numeric segment is present (pasted/odd links). */
function prLabel(row: ReportRow): string {
  try {
    const segments = new URL(row.url).pathname.split('/').filter((s) => /^\d+$/.test(s))
    if (segments.length) return `#${segments[segments.length - 1]}`
  } catch {
    /* not a parseable URL — fall through to the change_id */
  }
  return row.change_id || '#?'
}

/** Split a design-section value into scannable lines — mirrors report.py
 * `_design_facets`. The reviewer emits `solution_assessment` as short labeled
 * facets on SEPARATE LINES (see `review_driver.build_review_task`), but this view
 * used to hand the whole value to the markdown renderer, which collapsed them
 * into one dense paragraph — the archived HTML report split them and the app did
 * not. A record predating the structured prompt is one long prose blob, so that
 * case falls back to splitting on sentences rather than rendering a wall.
 *
 * `labeled` reports WHICH of those two splits produced the lines, because only a
 * real facet line may have a `Label:` prefix pulled out: prose with an early colon
 * ("The API returns 404: …") is a sentence, not a label, and treating it as one
 * crams the clause into the narrow label column.
 *
 * The sentence split deliberately avoids a lookbehind (`(?<=[.!?])`). WebKit only
 * shipped lookbehind in Safari 16.4, and an unsupported group is a SyntaxError at
 * MODULE EVALUATION — so it would take the whole app down on an older browser,
 * not just degrade this view. */
function designFacets(value: string): { lines: string[]; labeled: boolean } {
  const s = value.trim()
  const lines = s.split('\n').map((l) => l.replace(/^[\s\-•]+/, '').trim()).filter(Boolean)
  if (lines.length <= 1 && s.length > 160) {
    // A terminator is a boundary only at a word gap: `[.!?](?=\S)` keeps the
    // period inside `src/foo.py` and `v2.0` instead of fragmenting the token.
    const sentences = (s.match(/(?:[^.!?]|[.!?](?=\S))+[.!?]*/g) ?? [])
      .map((seg) => seg.trim())
      .filter(Boolean)
    return { lines: sentences, labeled: false }
  }
  return { lines: lines.length ? lines : (s ? [s] : []), labeled: true }
}

/** One facet line. On a real facet line a leading `Label:` prefix is pulled into
 * its own column so the facets read as a labeled list instead of running together
 * — mirrors `_facet_html`. Sentence-fallback lines are left whole. */
function Facet({ line, labeled }: { line: string; labeled: boolean }) {
  const m = labeled ? /^([A-Z][\w /&'+-]{1,40}):\s+(.*)$/.exec(line) : null
  if (!m) return <div className="text-[12.5px] leading-relaxed">{line}</div>
  return (
    <div className="flex gap-2.5 text-[12.5px] leading-relaxed">
      <span className="w-[96px] flex-shrink-0 font-semibold text-muted">{m[1]}</span>
      <span className="min-w-0 flex-1">{m[2]}</span>
    </div>
  )
}

/** The design narrative as a scannable chain — mirrors report.py `_design_html`:
 * the headline, then Problem / Why it matters / Solution fit, falling back to the
 * freeform rationale for records predating the structured fields. */
function DesignChain({ row }: { row: ReportRow }) {
  const steps: Array<[string, string | undefined]> = [
    [i18nT('apps.codeReviewSage.components.reportView.step_problem'), row.problem],
    [i18nT('apps.codeReviewSage.components.reportView.step_why_it_matters'), row.why_it_matters],
    [i18nT('apps.codeReviewSage.components.reportView.step_solution_fit'), row.solution_assessment],
  ]
  const present = steps.filter(([, v]) => v && v.trim())
  const hasHeadline = Boolean(row.design_headline && row.design_headline.trim())

  if (!hasHeadline && present.length === 0) {
    if (!row.rationale || !row.rationale.trim()) return null
    return <MarkdownRenderer content={row.rationale} />
  }

  return (
    <div className="space-y-3">
      {hasHeadline && (
        <div className="text-[13px] font-semibold leading-snug text-text-strong">
          <MarkdownRenderer content={row.design_headline as string} />
        </div>
      )}
      {present.map(([label, value]) => {
        const { lines: facets, labeled } = designFacets(value as string)
        return (
          <div key={label}>
            <SectionLabel>{label}</SectionLabel>
            {facets.length > 1 ? (
              <div className="space-y-1">
                {facets.map((line, i) => <Facet key={i} line={line} labeled={labeled} />)}
              </div>
            ) : (
              <MarkdownRenderer content={value as string} />
            )}
          </div>
        )
      })}
    </div>
  )
}

/** One report row: a summary header (expand button + PR link + badges + the
 * band rationale) over a collapsible detail area (design chain + findings). */
function ReportRowCard({
  row, postedKeys, isPosting, onPostFinding, selected, onToggleKey,
  fixSelected, onToggleFix, onFix,
}: {
  row: ReportRow
  postedKeys?: string[]
  /** Whether THIS comment is in flight. A single flag for the whole report made
   *  every unposted card claim it was posting when one finding was sent. */
  isPosting?: (key: string) => boolean
  onPostFinding?: (changeId: string, key: string) => void
  /** Keys ticked for a batched post, for THIS row's change. */
  selected?: Set<string>
  onToggleKey?: (changeId: string, key: string) => void
  fixSelected?: Set<string>
  onToggleFix?: (snapshot: ReviewFixFindingSnapshot) => void
  onFix?: (snapshot: ReviewFixFindingSnapshot) => void
}) {
  const [open, setOpen] = useState(false)
  const findings = row.findings ?? []
  const hasDetail = Boolean(
    row.design_headline || row.problem || row.why_it_matters
    || row.solution_assessment || row.rationale || findings.length,
  )
  const href = safeHttpUrl(row.url)

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex items-start gap-2 px-3.5 py-3">
        {/* The PR link is a sibling of the expand button, never nested inside it
            (an <a> inside a <button> is invalid interactive nesting). */}
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-[12.5px] text-accent hover:underline flex-shrink-0 pt-0.5"
            title={i18nT('apps.codeReviewSage.components.reportView.open_the_pull_request')}
          >
            {prLabel(row)}
          </a>
        ) : (
          <span className="font-mono text-[12.5px] text-muted flex-shrink-0 pt-0.5">{prLabel(row)}</span>
        )}

        <button
          type="button"
          aria-expanded={open}
          disabled={!hasDetail}
          onClick={() => setOpen((v) => !v)}
          className="group flex-1 min-w-0 text-left flex items-start gap-2 cursor-pointer disabled:cursor-default bg-transparent"
        >
          <span
            className={`${BAND_DOT[row.band]} mt-1 flex-shrink-0`}
            aria-hidden="true"
          >
            <span className="inline-block w-2 h-2 rounded-full bg-current" />
          </span>
          <span className="flex-1 min-w-0">
            <span className="block text-[13.5px] font-semibold text-text-strong leading-snug break-words">
              <MarkdownRenderer content={row.title} />
            </span>
            <span className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <Pill>{i18nT('apps.codeReviewSage.components.reportView.design_risk',
                { level: row.design_risk })}</Pill>
              <Pill>{i18nT('apps.codeReviewSage.components.reportView.blast_radius',
                { scope: row.blast })}</Pill>
              {row.red > 0 && (
                <span className="inline-flex items-center rounded-full bg-danger-subtle text-danger px-2 py-0.5 text-[11px] font-medium whitespace-nowrap">
                  {row.red} {i18nT('apps.codeReviewSage.components.reportView.blocking')}
                </span>
              )}
              {row.yellow > 0 && (
                <span className="inline-flex items-center rounded-full bg-warn-subtle text-warn px-2 py-0.5 text-[11px] font-medium whitespace-nowrap">
                  {row.yellow} {i18nT('apps.codeReviewSage.components.reportView.should_fix')}
                </span>
              )}
            </span>
            {/* `row.why` is deliberately NOT rendered. The backend builds it as scoring
                shorthand ("blast=LARGE + 1x red"), which restates the pills above in a
                form only someone who knows the model can read -- and being generated
                prose it cannot be translated for the other ten locales. */}
          </span>
          {hasDetail && (
            <ChevronRight
              size={16}
              className={`text-muted flex-shrink-0 mt-0.5 transition-transform ${open ? 'rotate-90' : ''}`}
              aria-hidden="true"
            />
          )}
        </button>
      </div>

      {open && hasDetail && (
        <div className="px-3.5 pb-3.5 pt-1 border-t border-border">
          <div className="rounded-lg border border-border bg-bg-elevated px-3.5 py-3 mb-2">
            <div className="flex items-center gap-1.5 mb-2">
              <ShieldCheck size={12} className="text-muted" aria-hidden="true" />
              <SectionLabel>{i18nT('apps.codeReviewSage.components.reportView.design_gate')} {row.gate_verdict}</SectionLabel>
            </div>
            <DesignChain row={row} />
          </div>
          {/* The verdict comment first: it is the one you decide about before the
              line-level notes. */}
          <ShipSummaryCard
            body={row.ship_comment ?? ''}
            posted={postedKeys?.includes(SHIP_KEY) ?? false}
            posting={!(postedKeys?.includes(SHIP_KEY) ?? false)
              && Boolean(isPosting?.(SHIP_KEY))}
            selectable={Boolean(onToggleKey)}
            selected={selected?.has(SHIP_KEY) ?? false}
            onToggle={onToggleKey ? () => onToggleKey(row.change_id, SHIP_KEY) : undefined}
            onPost={!(postedKeys?.includes(SHIP_KEY) ?? false) && onPostFinding
              ? () => onPostFinding(row.change_id, SHIP_KEY)
              : undefined}
          />
          {findings.map((f, i) => {
            // The key mirrors the backend's: findings keep the record's order, and
            // the report rows are generated from that same list, so the index is a
            // durable handle for one comment.
            const key = `finding:${i}`
            const snapshot = fixSnapshot(row, f, i)
            const sent = postedKeys?.includes(key) ?? false
            const fixable = row.band !== 'green'
            return (
              <FindingCard
                key={i}
                finding={f}
                posted={sent}
                posting={!sent && Boolean(isPosting?.(key))}
                selectable={Boolean(onToggleKey)}
                selected={selected?.has(key) ?? false}
                onToggle={onToggleKey ? () => onToggleKey(row.change_id, key) : undefined}
                fixSelectable={fixable && Boolean(onToggleFix || onFix)}
                fixSelected={fixSelected?.has(snapshot.key) ?? false}
                onToggleFix={onToggleFix ? () => onToggleFix(snapshot) : undefined}
                onFix={onFix ? () => onFix(snapshot) : undefined}
                label={f.file
                  ? i18nT('apps.codeReviewSage.components.reportView.finding_dimension_in_file',
                          { dimension: f.dimension, file: f.file })
                  : f.dimension}
                onPost={!sent && onPostFinding
                  ? () => onPostFinding(row.change_id, key)
                  : undefined}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

export default function ReportView({
  report, onArchive, archiving = false, archiveError = null, actions = null,
  postedKeys, isPosting, onPostFinding, onPostSelection, onStartFix,
}: {
  report: RunReport
  onArchive?: () => void
  archiving?: boolean
  archiveError?: string | null
  /** Comments already on the pull request, keyed by change id. */
  postedKeys?: Record<string, string[]>
  /** Whether a given comment key is in flight (see ReportRowCard). */
  isPosting?: (key: string) => boolean
  /** Post ONE finding. Omitted when this run cannot post. */
  onPostFinding?: (changeId: string, key: string) => void
  /** Post a chosen SET of comments. Grouped by change, because one request posts
   *  one pending review against one pull request.
   *
   *  MUST return the promise: the caller's result is what decides whether the
   *  ticks clear. A `void`-returning caller made `Promise.resolve(...)` succeed
   *  immediately, so the selection was wiped even when the post was refused and
   *  the comments never landed — which is the whole failure this await exists to
   *  prevent. Typed `Promise<void>` so that mistake cannot type-check. */
  onPostSelection?: (groups: { changeId: string; keys: string[] }[]) => Promise<void>
  /** Open the setup flow for immutable red/yellow finding snapshots. */
  onStartFix?: (findings: ReviewFixFindingSnapshot[]) => void
  /** Run-level actions (posting to the pull request) shown beside Share — the
   *  report is where you decide whether the findings are worth sending. */
  actions?: ReactNode
}) {
  const [active, setActive] = useState<Band | 'all'>('all')
  // Ticked comments, per change. Posting several together puts ONE pending review
  // on the pull request rather than one per comment, which is the difference the
  // author actually notices.
  const [selected, setSelected] = useState<Map<string, Set<string>>>(new Map())
  // Fix selection is a separate map so a repair request can never accidentally
  // become a GitHub draft, and vice versa.
  const [fixSelected, setFixSelected] = useState<Map<string, ReviewFixFindingSnapshot>>(new Map())
  // The header's overflow menu: it only exists while a selection is ticked (see
  // `selectionActive` below).
  const [overflowOpen, setOverflowOpen] = useState(false)

  const toggleKey = useCallback((changeId: string, key: string) => {
    setSelected((cur) => {
      const next = new Map(cur)
      const keys = new Set(next.get(changeId) ?? [])
      if (keys.has(key)) keys.delete(key)
      else keys.add(key)
      if (keys.size === 0) next.delete(changeId)
      else next.set(changeId, keys)
      return next
    })
  }, [])

  const selectedCount = useMemo(
    () => [...selected.values()].reduce((n, s) => n + s.size, 0),
    [selected],
  )

  const toggleFix = useCallback((snapshot: ReviewFixFindingSnapshot) => {
    setFixSelected((current) => {
      const next = new Map(current)
      if (next.has(snapshot.key)) next.delete(snapshot.key)
      else next.set(snapshot.key, snapshot)
      return next
    })
  }, [])

  const fixSelectedCount = fixSelected.size

  // A ticked selection turns the header into an action row, and the two primary
  // buttons already fill it — so the clears and the share affordance collapse
  // into one overflow menu while anything is ticked. Two buttons plus a kebab
  // is the cap this row can carry without degrading into a label scan.
  const selectionActive = selectedCount > 0 || fixSelectedCount > 0

  // Sort defensively (red → yellow → green, then by descending score) even
  // though the backend already emits them in this order, and narrow to the
  // active band when one is selected.
  const rows = useMemo(() => {
    const sorted = [...report.rows].sort(
      (a, b) => (BAND_RANK[a.band] - BAND_RANK[b.band]) || (b.score - a.score),
    )
    return active === 'all' ? sorted : sorted.filter((r) => r.band === active)
  }, [report.rows, active])

  // fmtDateTime, not toLocaleString: the raw call reads the BROWSER's locale, so a
  // translated report would still carry an English timestamp.
  const generatedLabel = report.generated_at ? fmtDateTime(report.generated_at) : ''

  return (
    <div className="flex flex-col gap-3">
      {/* Header line: total + generated_at, and the share affordance. */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="text-[12.5px] text-muted">
          {/* One key with the count inside it: the number and its noun
              inflect together, and some languages put them in the other
              order. */}
          {i18nT('apps.codeReviewSage.components.reportView.changes_reviewed', { count: report.total })}
          {generatedLabel && <span> {i18nT('apps.codeReviewSage.components.reportView.generated')} {generatedLabel}</span>}
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          {/* Posting and fixing are separate decisions. Either control may be
              visible at once, but neither selection can trigger the other. The
              two of them together are all the row can carry, so everything
              secondary (the clears, and Share when a selection hides it) moves
              into the overflow menu below. */}
          {selectedCount > 0 && onPostSelection && (
            <button
              type="button"
              onClick={() => {
                // Clear only once the post resolves. The grouped post is a
                // single request, but it can still be refused.
                Promise.resolve(onPostSelection([...selected.entries()].map(
                  ([changeId, keys]) => ({ changeId, keys: [...keys] }))))
                  .then(() => setSelected(new Map()))
                  .catch(() => { /* keep the ticks so the post can be retried */ })
              }}
              className="inline-flex items-center gap-1.5 rounded-md border border-accent bg-accent-subtle px-2.5 py-1 text-[12.5px] font-medium text-accent hover:bg-accent/20 cursor-pointer"
            >
              <MessageSquarePlus size={13} aria-hidden="true" />
              {i18nT('apps.codeReviewSage.components.reportView.draft_selected', { count: selectedCount })}
            </button>
          )}
          {/* `max-two-buttons-per-row` counts the overflow trigger as a button,
              so when both selections are active (comment + fix + trigger = 3)
              the fix action drops into the overflow menu; a single selection
              keeps its dedicated button for discoverability. */}
          {selectedCount === 0 && fixSelectedCount > 0 && onStartFix && (
            <button
              type="button"
              onClick={() => {
                onStartFix([...fixSelected.values()])
                setFixSelected(new Map())
              }}
              className="inline-flex items-center gap-1.5 rounded-md border border-accent bg-accent-subtle px-2.5 py-1 text-[12.5px] font-medium text-accent hover:bg-accent/20 cursor-pointer"
            >
              <Wrench size={13} aria-hidden="true" />
              {i18nT('apps.codeReviewSage.components.reportView.fix_selected', { count: fixSelectedCount })}
            </button>
          )}
          {selectionActive && (
            <DropdownMenu open={overflowOpen} onOpenChange={setOverflowOpen}>
              <DropdownMenuTrigger asChild>
                <Btn
                  type="button"
                  className="!px-1.5"
                  aria-label={i18nT('apps.codeReviewSage.components.reportView.more_actions')}
                  title={i18nT('apps.codeReviewSage.components.reportView.more_actions')}
                >
                  <MoreHorizontal size={14} className="lucide-inline" aria-hidden="true" />
                </Btn>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[190px]">
                {fixSelectedCount > 0 && onStartFix && (
                  <DropdownMenuItem
                    onSelect={() => {
                      onStartFix([...fixSelected.values()])
                      setFixSelected(new Map())
                    }}
                  >
                    <Wrench size={13} aria-hidden="true" />
                    {i18nT('apps.codeReviewSage.components.reportView.fix_selected', { count: fixSelectedCount })}
                  </DropdownMenuItem>
                )}
                {selectedCount > 0 && (
                  <DropdownMenuItem onSelect={() => setSelected(new Map())}>
                    {i18nT('apps.codeReviewSage.components.reportView.clear_comment_selection')}
                  </DropdownMenuItem>
                )}
                {fixSelectedCount > 0 && (
                  <DropdownMenuItem onSelect={() => setFixSelected(new Map())}>
                    {i18nT('apps.codeReviewSage.components.reportView.clear_fix_selection')}
                  </DropdownMenuItem>
                )}
                {!report.report_slug && onArchive && (
                  <DropdownMenuItem disabled={archiving} onSelect={onArchive}>
                    {archiving
                      ? <Loader2 size={13} className="animate-spin" aria-hidden="true" />
                      : <Share2 size={13} aria-hidden="true" />}
                    {archiving
                      ? i18nT('apps.codeReviewSage.components.reportView.sharing')
                      : i18nT('apps.codeReviewSage.components.reportView.share')}
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          {selectedCount === 0 && fixSelectedCount === 0 && actions}
          {report.report_slug ? (
            <a
              href={`/artifacts/${report.report_slug}`}
              className="inline-flex items-center gap-1.5 text-[12.5px] text-accent hover:underline"
            >
              <ExternalLink size={13} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.reportView.open_shared_copy')}
            </a>
          ) : !selectionActive && onArchive ? (
            <button
              type="button"
              onClick={onArchive}
              disabled={archiving}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1 text-[12.5px] text-text hover:text-accent hover:border-accent disabled:opacity-50 cursor-pointer disabled:cursor-default"
            >
              {archiving
                ? <Loader2 size={13} className="animate-spin" aria-hidden="true" />
                : <Share2 size={13} aria-hidden="true" />}
              {archiving
                ? i18nT('apps.codeReviewSage.components.reportView.sharing')
                : i18nT('apps.codeReviewSage.components.reportView.share')}
            </button>
          ) : null}
        </div>
      </div>

      {archiveError && (
        <div className="text-[12px] text-danger">{archiveError}</div>
      )}

      <BandChips bands={report.bands} active={active} onSelect={setActive} />

      {report.rows.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 py-16 text-center text-muted">
          <ClipboardCheck size={28} aria-hidden="true" />
          <div className="text-[13px]">{i18nT('apps.codeReviewSage.components.reportView.nothing_flagged_in_this_review')}</div>
        </div>
      ) : rows.length === 0 ? (
        <div className="py-8 text-center text-[12.5px] text-muted">
          {i18nT('apps.codeReviewSage.components.reportView.no_changes_in_this_band')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {rows.map((row) => (
            <ReportRowCard
              key={row.change_id || row.url}
              row={row}
              postedKeys={postedKeys?.[row.change_id]}
              isPosting={isPosting}
              onPostFinding={onPostFinding}
              selected={selected.get(row.change_id)}
              onToggleKey={onPostSelection ? toggleKey : undefined}
              fixSelected={new Set([...fixSelected.keys()].filter(key => key.startsWith(`${row.change_id}:`)))}
              onToggleFix={onStartFix ? toggleFix : undefined}
              onFix={onStartFix ? (snapshot) => onStartFix([snapshot]) : undefined}
            />
          ))}
        </div>
      )}
    </div>
  )
}
