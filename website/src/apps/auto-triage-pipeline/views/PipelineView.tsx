import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, RefreshCw, ChevronDown, Check } from 'lucide-react'
import {
  autoTriagePipelineApi, loadStoredPreference, saveRepoPreference,
  type CrewFabricItem, type RepoRef, type ConnectedRepo,
} from '../api'
import {
  SPINE_PHASES, foldItem, columnOccupancy, laneDwells, openDwellSeconds, formatDwell,
  queueSummary, laneTimelineRows, phaseHeader, phaseCaption,
  selectRepo, repoOrderKey, byteOrder,
  EDITING_SLOT_CAP, type FabricLane, type QueueSummary, type TimelineRow, type DwellParts,
} from '../lib/fabric'
import { Card, PageHeader, StatCard, IconButton, EmptyState as UIEmptyState } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import { fmtUnit } from '../../../i18n/format'

// The lane label for a work item: a localized "pull request" / "issue" prefix
// (other locales abbreviate these differently) joined to the number. The pure
// fold decides WHICH prefix from `prNumber`; the words are translated here.
function laneLabel(lane: FabricLane): string {
  // ALWAYS the issue number: a lane IS a crew work item, and a work item is keyed by
  // its issue. Prefixing `PR-` while interpolating that same issue number printed
  // `PR-5127` next to the card's own `#5179` PR chip -- two different numbers, one
  // labelled as the other. The PR is an attribute of the item, and the chip already
  // carries it, so the identity line states the identity and nothing else.
  return i18nT('apps.autoTriagePipeline.pipeline.lane_issue', { number: lane.number })
}

// Render a pure `{value, unit}` dwell as a localized string (`45s`, `12m`,
// `3.2h` in en; unit word + separator follow the active language elsewhere). The
// threshold decision already happened in `formatDwell`; this only localizes it.
function dwellText(parts: DwellParts): string {
  return fmtUnit(parts.value, parts.unit)
}

// Auto Triage Pipeline — one horizontal LANE per crew work item across the phase
// enum. An operator sees which phase each item is in, where items pile up, and
// which have stalled.
//
// STYLE: this page is built out of the shared UI kit (`components/ui`) and the
// Issue Radar list-row card idiom, NOT hand-rolled chrome. The lane drawing is
// the one bespoke element the mock justifies — but it now lives INSIDE that
// chrome: a compact ID card (Issue Radar's list-row shape) carries each item's
// id + title, and a right-sized track SVG draws only the spine, pads and dwell,
// scaling to fill the card's width. One content spine (`px-4 md:px-6`); no nested
// drawing frames.
//
// EVERY phase decision already happened in the pure, unit-tested `lib/fabric.ts`
// (head-is-live, exit-is-not-a-column, dwell-from-most-recent-entry). This file is
// PRESENTATION only — it never recomputes a phase, and the columns are the phase
// enum, so it cannot disagree with the ledger.
//
// COLOUR comes from theme CSS variables, never hardcoded hex (there is a
// lint:theme-colors gate).

// ── theme tokens (named once) ──
const C = {
  card: 'var(--card)',
  cardHl: 'var(--card-hl)',
  text: 'var(--text)',
  strong: 'var(--text-strong)',
  silk: 'var(--muted)',
  etch: 'var(--muted-strong)',
  line: 'var(--border)',
  line2: 'var(--border-strong)',
  go: 'var(--accent)',
  hold: 'var(--warn)',
  alarm: 'var(--danger)',
} as const

// ── RED SEMANTICS (defect #1/#2) ──────────────────────────────────────────────
// The live segment / head colour per phase class. `reply` (waiting on a human)
// and `exit` (a deliberate skip / yield / handed-back) are BENIGN — they must NOT
// wear the danger red a real fault does. `wait`/`reply` are amber (a holding
// state); `exit` is a quiet etch grey (a closed lane, out of the flow); `done` is
// etch too. Danger is reserved for a genuine fault and is not produced by any
// benign lane here. Border weight is uniform across every ID card (defect #2):
// "this lane exited" is carried by the exit stub and the muted tone, never by a
// heavier border.
const CLASS_COLOR: Record<FabricLane['cls'], string> = {
  edit: C.go,
  wait: C.hold,
  reply: C.hold,
  exit: C.etch,
  done: C.etch,
}
// Full literal KEYS, not translated strings: `i18nT()` at module scope runs once at
// import, before a language switch can reach it, so the words would freeze in
// whatever locale happened to be active when this module first loaded. Same `as const`
// shape as HEADER_KEY below, which keeps every key visible to the extractor and the
// unused-key tooling.
const CLASS_HEAD_WORD_KEY: Record<FabricLane['cls'], string> = {
  edit: 'apps.autoTriagePipeline.pipeline.head_editing',
  wait: 'apps.autoTriagePipeline.pipeline.head_waiting',
  reply: 'apps.autoTriagePipeline.pipeline.head_await_reply',
  exit: 'apps.autoTriagePipeline.pipeline.head_released',
  done: 'apps.autoTriagePipeline.pipeline.head_done',
}
const CARD_TAG_KEY: Record<FabricLane['cls'], string> = {
  edit: 'apps.autoTriagePipeline.pipeline.card_tag_editing',
  wait: 'apps.autoTriagePipeline.pipeline.card_tag_waiting',
  reply: 'apps.autoTriagePipeline.pipeline.card_tag_reply',
  exit: 'apps.autoTriagePipeline.pipeline.card_tag_exit',
  done: 'apps.autoTriagePipeline.pipeline.card_tag_done',
}

// FULL LITERAL catalog keys for the station header / caption / exit-token labels.
// The pure `phaseHeader` / `phaseCaption` / `exitToken` decide WHICH label a phase
// gets (the suffix, unit-tested in fabric.test.ts); these `as const` maps turn
// that suffix into a full literal key an extractor and the unused-key tooling can
// see — an interpolated `i18nT(`...${suffix}`)` key is invisible to both. Indexed
// by the suffix the pure function returns; a phase the pure function does not know
// (empty suffix) has no entry and the view falls back to the raw string.
const HEADER_KEY = {
  header_selected: 'apps.autoTriagePipeline.pipeline.header_selected',
  header_claimed: 'apps.autoTriagePipeline.pipeline.header_claimed',
  header_investigating: 'apps.autoTriagePipeline.pipeline.header_investigating',
  header_implementing: 'apps.autoTriagePipeline.pipeline.header_implementing',
  header_awaiting_ci: 'apps.autoTriagePipeline.pipeline.header_awaiting_ci',
  header_addressing_review: 'apps.autoTriagePipeline.pipeline.header_addressing_review',
  header_awaiting_merge: 'apps.autoTriagePipeline.pipeline.header_awaiting_merge',
  header_resolved: 'apps.autoTriagePipeline.pipeline.header_resolved',
} as const
const CAPTION_KEY = {
  caption_selected: 'apps.autoTriagePipeline.pipeline.caption_selected',
  caption_claimed: 'apps.autoTriagePipeline.pipeline.caption_claimed',
  caption_investigating: 'apps.autoTriagePipeline.pipeline.caption_investigating',
  caption_implementing: 'apps.autoTriagePipeline.pipeline.caption_implementing',
  caption_awaiting_ci: 'apps.autoTriagePipeline.pipeline.caption_awaiting_ci',
  caption_addressing_review: 'apps.autoTriagePipeline.pipeline.caption_addressing_review',
  caption_awaiting_merge: 'apps.autoTriagePipeline.pipeline.caption_awaiting_merge',
  caption_resolved: 'apps.autoTriagePipeline.pipeline.caption_resolved',
} as const
const EXIT_TOKEN_KEY = {
  exit_skipped: 'apps.autoTriagePipeline.pipeline.exit_skipped',
  exit_yielded: 'apps.autoTriagePipeline.pipeline.exit_yielded',
  exit_handed_back: 'apps.autoTriagePipeline.pipeline.exit_handed_back',
  exit_preempted: 'apps.autoTriagePipeline.pipeline.exit_preempted',
  exit_await_reply: 'apps.autoTriagePipeline.pipeline.exit_await_reply',
} as const

/** Translate a phase's station header via its literal catalog key. */
function headerText(phase: string): string {
  const key = HEADER_KEY[phaseHeader(phase) as keyof typeof HEADER_KEY]
  return key ? i18nT(key) : ''
}
/** Translate a phase's caption via its literal catalog key. */
function captionText(phase: string): string {
  const key = CAPTION_KEY[phaseCaption(phase) as keyof typeof CAPTION_KEY]
  return key ? i18nT(key) : ''
}
/** Translate an exit token via its literal catalog key; fall back to the raw
 * phase when the pure module produced no key (an unknown phase). */
function exitTokenText(token: string, phase: string): string {
  const key = EXIT_TOKEN_KEY[token as keyof typeof EXIT_TOKEN_KEY]
  return key ? i18nT(key) : phase
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches,
  )
  useEffect(() => {
    if (typeof matchMedia !== 'function') return
    const mq = matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches)
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])
  return reduced
}

/** Measure a track column's rendered width in CSS pixels, live. Returns [ref, w]:
 * attach `ref` to the element whose width the SVG viewBox should equal, and `w`
 * is that width (in px). Seeded with TRACK_FALLBACK_W so the first paint — before
 * layout / the ResizeObserver has reported — is not degenerate. Because the SVG's
 * viewBox width is set to this measured px width, the SVG X scale is 1 and glyphs
 * are never horizontally stretched at any container width. */
function useTrackWidth(): [React.MutableRefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null)
  const [w, setW] = useState<number>(TRACK_FALLBACK_W)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver !== 'function') return
    const apply = (px: number) => {
      // guard against a 0/degenerate measurement (hidden tab, first frame)
      if (px > 1) setW(px)
    }
    apply(el.getBoundingClientRect().width)
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) apply(e.contentRect.width)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, w]
}

/** The whole page. Resolves which repo to show from the BACKEND's connected-repo
 * list (source of truth) combined with a remembered preference, so the app stands
 * alone. */
export default function PipelineView() {
  const reposQuery = useQuery({
    queryKey: ['auto-triage-pipeline', 'connected-repos'],
    queryFn: () => autoTriagePipelineApi.listConnectedRepos(),
    refetchOnWindowFocus: true,
  })
  const connected = useMemo<ConnectedRepo[]>(
    () => (Array.isArray(reposQuery.data) ? reposQuery.data : []),
    [reposQuery.data],
  )

  const [chosen, setChosen] = useState<RepoRef | null>(null)
  const resolved = useMemo(
    () => selectRepo(chosen ?? loadStoredPreference(), connected),
    [chosen, connected],
  )

  const onPick = useCallback((ref: RepoRef) => {
    saveRepoPreference(ref)
    setChosen(ref)
  }, [])

  if (reposQuery.isLoading && !resolved) return <ResolvingState />
  if (!resolved) return <NoRepoState />
  return (
    <PipelineDashboard
      key={`${resolved.provider ?? 'github'}:${resolved.host ?? ''}:${resolved.owner}/${resolved.repo}`}
      repo={resolved}
      connected={connected}
      onPick={onPick}
    />
  )
}

/** The header shared by every state — the shared PageHeader with a single title
 * (the app's own name, so the page announces ONE name, not three — defect #20),
 * the repo picker, and the refresh control living WITH the repo it acts on
 * (defect #23). */
function PageChrome({
  repo, connected, onPick, onRefresh, refreshing,
}: {
  repo?: RepoRef
  connected?: ConnectedRepo[]
  onPick?: (ref: RepoRef) => void
  onRefresh?: () => void
  refreshing?: boolean
}) {
  return (
    <PageHeader
      title={
        <span className="inline-flex items-center gap-2">
          <Activity size={20} className="text-accent" />
          {i18nT('apps.autoTriagePipeline.manifest.page_label')}
        </span>
      }
      subtitle={i18nT('apps.autoTriagePipeline.pipeline.subtitle')}
      actions={
        repo && connected && onPick ? (
          <div className="flex items-center gap-2">
            <RepoPicker repo={repo} connected={connected} onPick={onPick} />
            {onRefresh && (
              <IconButton
                aria-label={i18nT('apps.autoTriagePipeline.pipeline.refresh')}
                title={i18nT('apps.autoTriagePipeline.pipeline.refresh')}
                onClick={onRefresh}
                disabled={refreshing}
                className="border border-border h-8 w-8 flex items-center justify-center"
              >
                <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
              </IconButton>
            )}
          </div>
        ) : undefined
      }
    />
  )
}

function PipelineDashboard({
  repo, connected, onPick,
}: {
  repo: RepoRef
  connected: ConnectedRepo[]
  onPick: (ref: RepoRef) => void
}) {
  const reduced = useReducedMotion()
  const [hovered, setHovered] = useState<number | null>(null)

  const scopeKey = `${repo.provider ?? 'github'}:${repo.host ?? ''}:${repo.owner}/${repo.repo}`
  const fabricQuery = useQuery({
    queryKey: ['auto-triage-pipeline', 'fabric', scopeKey],
    queryFn: () => autoTriagePipelineApi.crewFabric(repo),
    refetchInterval: 30_000,
  })

  const items = useMemo<CrewFabricItem[]>(
    () => (Array.isArray(fabricQuery.data?.items) ? fabricQuery.data.items : []),
    [fabricQuery.data],
  )
  const lanes = useMemo(() => items.map(foldItem), [items])
  const occupancy = useMemo(() => columnOccupancy(lanes), [lanes])

  const generatedAt = fabricQuery.data?.generated_at ?? null
  const nowMs = useMemo(() => {
    const t = generatedAt ? new Date(generatedAt).getTime() : NaN
    return Number.isNaN(t) ? Date.now() : t
  }, [generatedAt])

  const summary = useMemo(() => queueSummary(lanes, nowMs), [lanes, nowMs])
  const loading = fabricQuery.isLoading

  return (
    <div className="h-full overflow-y-auto pb-6">
      <PageChrome
        repo={repo}
        connected={connected}
        onPick={onPick}
        onRefresh={() => fabricQuery.refetch()}
        refreshing={fabricQuery.isFetching}
      />

      <div className="px-4 md:px-6 flex flex-col gap-4">
        {/* THE QUEUE SUMMARY — reads first: "how is the queue doing". Built from
            shared StatCards so its padding/rhythm matches the rest of the page. */}
        <QueueSummaryCards summary={summary} hasData={lanes.length > 0} />

        {/* THE LANE BOARD — one compact card per item, then its right-sized track. */}
        {loading && lanes.length === 0 ? (
          <Card><LaneBoardSkeleton /></Card>
        ) : lanes.length === 0 ? (
          <EmptyState />
        ) : (
          <LaneBoard
            lanes={lanes}
            occupancy={occupancy}
            summary={summary}
            nowMs={nowMs}
            generatedAt={generatedAt}
            repoLabel={`${repo.owner}/${repo.repo}`}
            reduced={reduced}
            hovered={hovered}
            onHover={setHovered}
          />
        )}
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// THE QUEUE SUMMARY — shared StatCards. One row of headline gauges. The per-phase
// counts move into the board's own column header, so nothing here is clipped
// against a card edge (defect #5) and every card shares one padding scale
// (defect #22).
// ─────────────────────────────────────────────────────────────────────────────

function QueueSummaryCards({ summary, hasData }: { summary: QueueSummary; hasData: boolean }) {
  const longest = summary.longestWait
  const cards: Array<{ label: string; value: string; colorClass?: string; title?: string }> = [
    {
      label: i18nT('apps.autoTriagePipeline.pipeline.summary_in_flight'),
      value: String(summary.live),
    },
    {
      label: i18nT('apps.autoTriagePipeline.pipeline.summary_editing'),
      // The cap is PER CREW, so the total is the informative number and the fault
      // condition is "some crew holds more than one". Showing `n / 1` in danger red
      // called two crews working normally a violation.
      value: `${summary.editing}`,
      colorClass: summary.editingMaxPerCrew > EDITING_SLOT_CAP
        ? 'text-danger'
        : summary.editing ? 'text-accent' : undefined,
      title: i18nT('apps.autoTriagePipeline.pipeline.dashboard_editing_cap'),
    },
    {
      label: i18nT('apps.autoTriagePipeline.pipeline.summary_reopens'),
      value: String(summary.reopens),
      colorClass: summary.reopens ? 'text-warn' : undefined,
    },
    {
      label: i18nT('apps.autoTriagePipeline.pipeline.summary_longest_wait'),
      value: longest ? dwellText(formatDwell(longest.seconds)) : '—',
      colorClass: longest && longest.seconds > 3600 ? 'text-warn' : undefined,
      title: longest
        ? i18nT('apps.autoTriagePipeline.pipeline.summary_longest_wait_in', { phase: longest.phase })
        : i18nT('apps.autoTriagePipeline.pipeline.summary_longest_wait_none'),
    },
  ]

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      {cards.map((c, i) => (
        <StatCard
          key={c.label}
          label={c.label}
          value={hasData ? c.value : '—'}
          colorClass={c.colorClass}
          title={c.title}
          delay={i * 40}
          className="mb-0"
        />
      ))}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// THE LANE BOARD — the drawing, inside a shared Card. A fixed-width ID-card
// column on the left (Issue Radar's list-row shape) and a fluid track column on
// the right whose SVG scales to fill the card. One shared column header carries
// the phase names + occupancy so every lane aligns to the same grid.
// ─────────────────────────────────────────────────────────────────────────────

// The track's own coordinate space. The SVG's viewBox width is set EQUAL to the
// track's measured layout width in CSS pixels, so with `width="100%"` the X scale
// is exactly 1 (one user unit = one CSS pixel) and glyphs are never distorted — a
// glyph's on-screen aspect ratio equals its aspect ratio in the font at any
// container width. The columns still span the full track because their x's are
// computed FROM that same width (see `colVbX(i, width)`), not from a fixed
// constant that letterboxes. `preserveAspectRatio="none"` is retained: with a
// matched width the X scale is 1 regardless, and it keeps the Y independent of the
// (unused) aspect so the fixed 34px height is honoured. `overflow: visible` lets
// the trailing dwell / exit labels spill past the last column without needing a
// wide right reservation (which is what left RESOLVED stranded at ~83% with dead
// space beyond it — defect #16). The lane pitch is tight (defect #19).
//
// TRACK_FALLBACK_W is only the FIRST-PAINT width used before the ResizeObserver
// reports the real one; it must be non-degenerate so the initial frame is not
// collapsed. Every position is a function of the live measured width thereafter.
const TRACK_FALLBACK_W = 720
const TRACK_H = 34
// Symmetric, small end inset in PIXELS. Both the first (SELECTED) and last
// (RESOLVED) station labels are centred on their column, so each needs a little
// clearance from the track edge to sit fully inside the card — but only a little.
// The old asymmetric 6/118 pad stranded RESOLVED at ~83% with a dead sixth beyond
// it (defect #16); a small fixed inset each side keeps the spine across nearly the
// whole width with room for the end labels, and `overflow: visible` on the SVG
// covers the few px an exit stub or open-dwell spills past the head.
export const TRACK_PAD_L = 30
export const TRACK_PAD_R = 30
const ID_CARD_W = 176 // compact: id + title, no dead gap (defects #14/#17)

/** Below this MEASURED track width the drawing stops being a drawing.
 *
 * Nine phase columns each need room for a label like `INVESTIG.` at the 10px
 * accessibility floor; squeezed under that they overprint. Measured at a 320px
 * viewport with the card beside the track: the header row rendered as a single
 * black smear and the dwell numbers overprinted each other (`3.2h` over `10m`).
 *
 * Gated on the width the ResizeObserver reports for the TRACK, never on a viewport
 * breakpoint -- per the repo's page-layout note, a 1280px window can hold a 200px
 * pane, so the constraint is the pane and `useIsMobile()` would answer the wrong
 * question. */
const NARROW_TRACK_W = 560

const colFrac = (i: number) => i / (SPINE_PHASES.length - 1)
// The x of column `i` inside a track of `width` px. Pure and width-parameterised
// so the same maths drives both the SVG geometry and the HTML header labels, and
// so it can be unit-tested: the columns span [TRACK_PAD_L, width - TRACK_PAD_R],
// i.e. the FULL width less the symmetric end inset, at any width.
export const colVbX = (i: number, width: number) =>
  TRACK_PAD_L + colFrac(i) * (width - TRACK_PAD_L - TRACK_PAD_R)

function LaneBoard({
  lanes, occupancy, summary, nowMs, generatedAt, repoLabel, reduced, hovered, onHover,
}: {
  lanes: FabricLane[]
  occupancy: Map<number, { total: number; editing: number }>
  summary: QueueSummary
  nowMs: number
  generatedAt: string | null
  repoLabel: string
  reduced: boolean
  hovered: number | null
  onHover: (n: number | null) => void
}) {
  const [card, setCard] = useState<{ lane: FabricLane; x: number; y: number } | null>(null)
  const boardRef = useRef<HTMLDivElement | null>(null)
  // ONE measurement drives every row: the track column geometry is identical
  // across the header and every lane row (same `flex-1 min-w-0` + `pl-3`), so we
  // measure the header's track region and thread that px width down. The SVG
  // viewBox width is set equal to it, making the X scale 1 (no glyph stretch).
  const [trackRef, trackW] = useTrackWidth()
  // Measured, not a breakpoint. `trackW` starts at TRACK_FALLBACK_W and the observer
  // applies the real width synchronously in its effect, so this is wide for at most
  // one frame rather than flashing the narrow layout on a desktop.
  const narrow = trackW < NARROW_TRACK_W

  const generatedLabel = generatedAt
    ? new Date(generatedAt).toISOString().slice(11, 16) + 'Z'
    : '—'

  const onRowMove = useCallback((lane: FabricLane, ev: React.MouseEvent) => {
    const host = boardRef.current
    if (!host) return
    const r = host.getBoundingClientRect()
    setCard({ lane, x: ev.clientX - r.left, y: ev.clientY - r.top })
    onHover(lane.number)
  }, [onHover])
  const onRowLeave = useCallback(() => {
    setCard(null)
    onHover(null)
  }, [onHover])

  return (
    <Card className="p-0 mb-0">
      <div ref={boardRef} className="relative">
        {/* card meta strip — repo / items / in-flight / folded — replaces the
            SVG title block; no third indent (defect #9), no name repeat (#20). */}
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 px-3 md:px-4 pt-3 pb-2 text-[11px] text-muted border-b border-border">
          <MetaItem label={i18nT('apps.autoTriagePipeline.pipeline.titleblock_repo')} value={repoLabel} strong />
          <MetaItem label={i18nT('apps.autoTriagePipeline.pipeline.titleblock_items')} value={String(lanes.length)} />
          <MetaItem label={i18nT('apps.autoTriagePipeline.pipeline.titleblock_live')} value={String(summary.live)} />
          <MetaItem label={i18nT('apps.autoTriagePipeline.pipeline.titleblock_generated')} value={generatedLabel} />
          <span className="ml-auto text-[10px] tracking-[.18em] text-muted-strong uppercase">
            {i18nT('apps.autoTriagePipeline.pipeline.fence_label')}
          </span>
        </div>

        {/* The shared column header is the drawing's legend, so it renders only when
            the drawing does. At a narrow width the labels have no room and each row
            names its own phase in text instead. */}
        <ColumnHeader occupancy={occupancy} trackRef={trackRef} trackW={trackW} narrow={narrow} />

        {/* one row per work item: compact ID card + fluid track (stacked when narrow) */}
        <ul className="flex flex-col">
          {lanes.map((lane) => (
            <LaneRow
              key={lane.number}
              lane={lane}
              nowMs={nowMs}
              reduced={reduced}
              trackW={trackW}
              narrow={narrow}
              dimmed={hovered != null && hovered !== lane.number}
              onMove={(ev) => onRowMove(lane, ev)}
              onLeave={onRowLeave}
            />
          ))}
        </ul>

        {card && <HoverCard lane={card.lane} x={card.x} y={card.y} nowMs={nowMs} />}
      </div>

      {/* NOTES + legend — folded in right under the drawing they explain
          (defects #3/#18/#21) */}
      <NotesPanel />
    </Card>
  )
}

function MetaItem({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-[10px] tracking-[.12em] text-muted-strong uppercase">{label}</span>
      <span className={`font-mono tabular-nums ${strong ? 'text-text-strong' : 'text-text'}`}>{value}</span>
    </span>
  )
}

/** The shared phase-column header: the grid the ID card and every track align to.
 * A left spacer the exact width of the ID card, then the phase names spread across
 * the track's fraction positions, each with its live-occupancy count under it.
 * This is the ONE header band (defect #11 — every column has the same height;
 * defect #10 — captions share a baseline). */
function ColumnHeader({
  occupancy, trackRef, trackW, narrow,
}: {
  occupancy: Map<number, { total: number; editing: number }>
  trackRef: React.MutableRefObject<HTMLDivElement | null>
  trackW: number
  narrow: boolean
}) {
  // The header carries the ONLY element `useTrackWidth` observes, so it must stay
  // mounted even when its labels are useless. Unmounting it at narrow detaches the
  // observer, which freezes `trackW` at its last value: the layout would enter narrow
  // correctly and then never leave it, because nothing is left to report that the pane
  // grew again.
  //
  // It also keeps the WIDE geometry while narrow -- the ID-card spacer and the same
  // `pl-3` inset -- on purpose. Measure the narrow track instead and the number feeding
  // the decision changes with the decision: a wide track is `pane - 188` and a narrow
  // one `pane - 24`, so a pane around 700px reads NARROW while wide and WIDE while
  // narrow, and the layout flips forever. Measuring the wide geometry in both branches
  // makes `trackW` mean one fixed thing -- the width a wide track would have -- so the
  // gate has a stable input. The narrow track is then a little wider than the viewBox
  // says, which stretches the drawing horizontally and is harmless precisely because
  // every `<text>` in the track is suppressed at narrow.
  if (narrow) {
    return (
      <div className="flex items-stretch px-3 md:px-4" aria-hidden="true">
        <div style={{ width: ID_CARD_W }} className="flex-shrink-0" />
        <div className="flex-1 min-w-0 pl-3">
          <div ref={trackRef} className="h-0" />
        </div>
      </div>
    )
  }
  return (
    <div className="flex items-stretch px-3 md:px-4 pt-2 pb-2 border-b border-border bg-bg-elevated/40">
      <div style={{ width: ID_CARD_W }} className="flex-shrink-0 flex items-end">
        <span className="text-[10px] tracking-[.14em] text-muted-strong uppercase">
          {i18nT('apps.autoTriagePipeline.pipeline.dashboard_per_phase')}
        </span>
      </div>
      {/* The band is sized to hold THREE stacked rows — phase name, live count,
          and the "piling up" flag — with room to spare, so the flag is never
          cramped against the card edge or the header border (defect #5). The tick
          SVG is a fixed 56-unit-tall backdrop the labels sit over. `pl-3` and the
          `trackRef` content box match a LaneRow's track geometry exactly, so the
          measured width the SVG viewBox uses is the width the row SVGs render at
          and every column lines up. */}
      <div ref={trackRef} className="relative flex-1 min-w-0 min-h-[56px] pl-3">
        <svg
          viewBox={`0 0 ${trackW} 56`}
          width="100%"
          height="56"
          preserveAspectRatio="none"
          aria-hidden="true"
          className="block"
        >
          {SPINE_PHASES.map((phase, i) => (
            <line key={phase} x1={colVbX(i, trackW)} y1={2} x2={colVbX(i, trackW)} y2={54} stroke={C.line2} strokeDasharray="1 4" opacity={0.7} />
          ))}
        </svg>
        <div className="absolute inset-0 left-3">
          {SPINE_PHASES.map((phase, i) => {
            const occ = occupancy.get(i)
            const leftPct = (colVbX(i, trackW) / trackW) * 100
            return (
              <div
                key={phase}
                className="absolute top-0 -translate-x-1/2 flex flex-col items-center gap-0.5 w-[72px] pt-0.5"
                style={{ left: `${leftPct}%` }}
                title={captionText(phase)}
              >
                <span className="text-[10px] font-bold tracking-[.08em] text-text text-center leading-tight">
                  {headerText(phase)}
                </span>
                {occ ? (
                  <span
                    className="text-[10px] font-bold tabular-nums"
                    style={{ color: occ.editing ? C.go : C.hold }}
                  >
                    {occ.total}
                  </span>
                ) : (
                  <span className="text-[10px] text-muted-strong">·</span>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ── one lane: a compact ID card + a fluid track ──

function LaneRow({
  lane, nowMs, reduced, trackW, narrow, dimmed, onMove, onLeave,
}: {
  lane: FabricLane
  nowMs: number
  reduced: boolean
  trackW: number
  narrow: boolean
  dimmed: boolean
  onMove: (ev: React.MouseEvent) => void
  onLeave: () => void
}) {
  const color = CLASS_COLOR[lane.cls]
  const ghost = lane.cls === 'done'
  const label = laneLabel(lane)

  return (
    // The mouse listeners only reveal a REDUNDANT hover tooltip (the full title
    // is already in the ID card, the dwell already on the track); the row has no
    // click action and gates no content behind hover, so it is a pointer-only
    // enhancement, not an interaction a keyboard user is denied.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <li
      className={`flex ${narrow ? 'flex-col gap-1.5' : 'items-stretch'} gap-0 px-3 md:px-4 py-1.5 border-b border-border last:border-b-0 transition-opacity`}
      style={{ opacity: dimmed ? 0.3 : 1, cursor: 'default' }}
      onMouseMove={onMove}
      onMouseLeave={onLeave}
    >
      {/* THE COMPACT ID CARD — copies Issue Radar's list-row idiom verbatim
          (IssueList/CrewList `cardClass` + `cardInner`): `rounded-lg border p-2.5
          bg-card`, a meta row with the number in `font-bold text-accent` and a
          right-aligned marker, the title on a `line-clamp` line, then a chips row.
          Copied rather than imported: Issue Radar's row is a `<button>`/`Clickable`
          bound to its `useIssueRadar()` selection context and its own `Issue`/`Crew`
          shape, so the STYLE is reused and the data is this app's own. Border weight
          is uniform regardless of exit (defect #2); the number + state chip + PR
          chip say what the item IS (defect #4); ~176px compact (defect #17). */}
      <div
        style={narrow ? undefined : { width: ID_CARD_W }}
        className={`${narrow ? 'w-full' : 'flex-shrink-0'} rounded-lg border border-border bg-card p-2.5 flex flex-col justify-center`}
      >
        {/* meta row — Issue Radar's `#number` in accent bold, state chip on the
            right (Issue Radar puts the relative time here; a lane's live state is
            the more useful right-aligned token). */}
        <div className="flex items-center justify-between gap-1.5 text-[12px] text-muted">
          <span className="font-bold text-accent tabular-nums truncate">{label}</span>
          <span
            className="text-[10px] tracking-[.1em] uppercase px-1 py-px rounded-sm border flex-shrink-0 leading-none"
            style={{ color, borderColor: color }}
          >
            {i18nT(CLASS_HEAD_WORD_KEY[lane.cls])}
          </span>
        </div>
        {/* title line — Issue Radar's `line-clamp-2 text-text`. The seeded fixtures
            carry no title, so this renders only when one is present; the number +
            chips still say what the item is when it is absent. */}
        {lane.title ? (
          <div className="text-[12px] leading-snug text-text line-clamp-2 mt-1" title={lane.title}>
            {lane.title}
          </div>
        ) : null}
        {/* chips row — Issue Radar's label-chip row. Carries the PR pill (chamfered
            chip vs plain lane) and any reopen count, as pill chips rather than a
            bare `▸ #` line. */}
        {(lane.prNumber != null || lane.reopens > 0) && (
          <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
            {lane.prNumber != null && (
              <span className="inline-flex items-center rounded-full font-medium px-1.5 py-0 text-[10px] bg-bg-hover text-muted tabular-nums">
                {`#${lane.prNumber}`}
              </span>
            )}
            {lane.reopens > 0 && (
              <span
                className="inline-flex items-center rounded-full font-medium px-1.5 py-0 text-[10px]"
                style={{ backgroundColor: 'var(--warn-subtle)', color: C.hold }}
              >
                {i18nT('apps.autoTriagePipeline.pipeline.card_reopen', { count: lane.reopens })}
              </span>
            )}
          </div>
        )}
      </div>

      {/* THE FLUID TRACK — scales to fill the remaining width, columns aligned to
          the shared header grid. Stacked under the card when narrow, where it gets
          the full pane width instead of what is left beside a 176px card. */}
      <div className={`flex-1 min-w-0 flex items-center ${narrow ? '' : 'pl-3'}`}>
        <LaneTrack lane={lane} nowMs={nowMs} reduced={reduced} trackW={trackW} color={color} ghost={ghost} narrow={narrow} />
      </div>

      {/* At a narrow width the column legend is gone, so the lane names its own
          position in words: which phase it is in, how far along the spine that is,
          and the open dwell. Composed from keys the wide layout already uses -- the
          phase's own header label and `and_counting` -- so narrow adds no string a
          translator has not already seen. */}
      {narrow && (
        <div className="flex items-center gap-1.5 text-[10px] text-muted tabular-nums flex-wrap">
          {/* An exited lane names WHICH exit. `headerText` only speaks the spine, and
              the card's state chip collapses skipped / yielded / handed-back /
              preempted into one word, so without this the narrow layout could not
              tell them apart at all. */}
          <span className="uppercase tracking-[.1em]" style={{ color }}>
            {lane.exit
              ? exitTokenText(lane.exit.token, lane.exit.phase)
              : headerText(lane.phase)}
          </span>
          {lane.head >= 0 && <span>{`${lane.head + 1}/${SPINE_PHASES.length}`}</span>}
          {(() => {
            const open = openDwellSeconds(lane, nowMs)
            return open != null && lane.cls !== 'done' ? (
              <span style={{ color: open > 3600 ? C.hold : undefined }}>
                {i18nT('apps.autoTriagePipeline.pipeline.and_counting', { dwell: `+${dwellText(formatDwell(open))}` })}
              </span>
            ) : null
          })()}
        </div>
      )}
    </li>
  )
}

/** The per-lane track SVG: the traversed spine, a pad per reached phase, the live
 * coloured segment, dwell labels between pads, the head word, and an exit stub.
 * `width="100%"` on a fixed viewBox, so it fills its column and its columns line
 * up with the header. */
function LaneTrack({
  lane, nowMs, reduced, trackW, color, ghost, narrow,
}: {
  lane: FabricLane
  nowMs: number
  reduced: boolean
  trackW: number
  color: string
  ghost: boolean
  narrow: boolean
}) {
  const y = TRACK_H / 2
  const spineEnd = colVbX(SPINE_PHASES.length - 1, trackW)
  const laneStart = TRACK_PAD_L
  const firstReached = lane.reach.length ? colVbX(lane.reach[0], trackW) : laneStart
  const prevX = lane.prevIdx >= 0 ? colVbX(lane.prevIdx, trackW) : laneStart
  const dwells = laneDwells(lane)
  const openDwell = openDwellSeconds(lane, nowMs)
  const exitAtX = lane.exit
    ? (lane.exit.atColumn >= 0 ? colVbX(lane.exit.atColumn, trackW) : firstReached)
    : 0
  // awaiting-reply is BENIGN (amber); a genuine released/skip exit is quiet etch.
  const exitColor = lane.exit?.phase === 'awaiting-reply' ? C.hold : C.etch

  return (
    <svg
      viewBox={`0 0 ${trackW} ${TRACK_H}`}
      width="100%"
      height={TRACK_H}
      preserveAspectRatio="none"
      role="img"
      aria-label={i18nT('apps.autoTriagePipeline.pipeline.aria_diagram')}
      style={{ display: 'block', fontFamily: 'var(--mono, ui-monospace, monospace)', overflow: 'visible' }}
    >
      {/* base rail across all columns */}
      <line x1={laneStart} y1={y} x2={spineEnd} y2={y} stroke={C.line2} strokeDasharray="1 5" opacity={0.65} />

      {lane.head >= 0 && (
        <>
          {/* un-reached tail (faint) after the head */}
          <line x1={colVbX(lane.head, trackW)} y1={y} x2={spineEnd} y2={y} stroke={C.line} strokeDasharray="2 5" />
          {/* lead-in to the first reached column */}
          <line x1={laneStart} y1={y} x2={firstReached} y2={y} stroke={ghost ? C.etch : C.line2} strokeWidth={1.4} />
          {/* traversed spine; dot-dash where a phase was bypassed */}
          {lane.reach.slice(1).map((b, k) => {
            const a = lane.reach[k]
            const skipped = b > a + 1
            return (
              <line
                key={`${a}-${b}`}
                x1={colVbX(a, trackW)} y1={y} x2={colVbX(b, trackW)} y2={y}
                stroke={ghost ? C.etch : C.line2}
                strokeWidth={skipped ? 1.1 : 1.4}
                strokeDasharray={skipped ? '7 3 2 3' : undefined}
              />
            )
          })}
          {/* the LIVE segment carries the phase-class hue */}
          <line x1={prevX} y1={y} x2={colVbX(lane.head, trackW)} y2={y} stroke={color} strokeWidth={2} opacity={ghost ? 0.6 : 0.95} />
          {/* a pad at each reached phase */}
          {lane.reach.map((r) => (
            <rect key={r} x={colVbX(r, trackW) - 2.5} y={y - 2.5} width={5} height={5} fill={r === lane.head ? color : ghost ? C.etch : C.line2} />
          ))}
          {/* return traces: a re-entered phase (round-trip / reopen), well below
              the rail so they clear the open-dwell label (defect #6/#13) */}
          {lane.loops.map((lp, li) => (
            <path
              key={`${lp.from}-${lp.to}-${li}`}
              d={`M${colVbX(lp.from, trackW)} ${y + 3} V${y + 8 + li * 4} H${colVbX(lp.to, trackW)} V${y + 3}`}
              fill="none" stroke={C.hold} strokeWidth={1.1} strokeDasharray="4 3" opacity={0.7}
            />
          ))}
          {/* head pulse (reduced-motion off) */}
          {lane.cls === 'edit' && !reduced && (
            <circle r={2.4} fill={C.go}>
              <animateMotion dur="2.2s" repeatCount="indefinite" path={`M${prevX} ${y} H${colVbX(lane.head, trackW)}`} />
            </circle>
          )}
          {(lane.cls === 'wait' || lane.cls === 'reply') && !reduced && (
            <circle cx={colVbX(lane.head, trackW)} cy={y} r={2.2} fill={C.hold}>
              <animate attributeName="opacity" values="0.5;1;0.5" dur="1.8s" repeatCount="indefinite" />
            </circle>
          )}
          {/* NO head word on the track: the item's live state is already on the
              ID card's tag (EDITING / WAITING / AWAIT REPLY / RELEASED), so
              repeating it here only crowds the head and collided with the last
              segment's dwell label (defect #6). The coloured head pad carries the
              position; the dwell carries the timing. */}
          {/* per-segment dwell, centred between pads, ABOVE the rail. Segment
              dwells and the open-dwell live on OPPOSITE sides of the rail with a
              wide vertical gap between their bands, so they cannot collide at any
              track width even when horizontal compression brings their x's close
              (defects #6/#13). */}
          {!narrow && dwells.map((d) => {
            const from = lane.reach[lane.reach.indexOf(d.toColumn) - 1]
            const midX = (colVbX(from, trackW) + colVbX(d.toColumn, trackW)) / 2
            return (
              <text key={d.toColumn} x={midX} y={y - 7} textAnchor="middle" fontSize={10} fill={d.seconds > 3600 ? C.hold : C.etch}>
                {dwellText(formatDwell(d.seconds))}
              </text>
            )
          })}
          {/* the still-open dwell, to the right of the head and well BELOW the
              rail — a different band from the segment dwells above (defect #6) and
              clear of the loop arcs, which only ever run at or left of the head. */}
          {!narrow && openDwell != null && lane.cls !== 'done' && (
            <text x={colVbX(lane.head, trackW) + 8} y={y + 17} fontSize={10} fontWeight={700} fill={openDwell > 3600 ? C.hold : color}>
              {i18nT('apps.autoTriagePipeline.pipeline.dwell_open', { dwell: dwellText(formatDwell(openDwell)) })}
            </text>
          )}
        </>
      )}

      {/* exit stub: drawn OFF the lane, quiet — never the danger colour for a
          benign exit (defect #1) */}
      {lane.exit && (
        <>
          <path
            d={`M${exitAtX} ${y + 3} v9 h18`}
            stroke={exitColor}
            strokeWidth={1.3} fill="none"
          />
          <text
            x={exitAtX + 22} y={y + 15} fontSize={10} fontWeight={700} letterSpacing=".08em"
            fill={exitColor}
          >
            {narrow ? '' : exitTokenText(lane.exit.token, lane.exit.phase)}
          </text>
        </>
      )}
    </svg>
  )
}

// ── the hover card: the full phase table + per-phase dwell (flags >1h) ──

function HoverCard({ lane, x, y, nowMs }: { lane: FabricLane; x: number; y: number; nowMs: number }) {
  const rows: TimelineRow[] = laneTimelineRows(lane)
  const label = laneLabel(lane)
  const openDwell = openDwellSeconds(lane, nowMs)
  const tagColor = CLASS_COLOR[lane.cls]
  const hhmm = (ms: number | null) => (ms == null ? '—' : new Date(ms).toISOString().slice(11, 16))

  const CARD_W = 340
  return (
    <div
      role="tooltip"
      className="absolute z-50 pointer-events-none rounded-md border border-border-strong bg-card shadow-lg px-3.5 py-3 text-[11px]"
      style={{ left: Math.max(4, x + 14), top: y + 14, width: CARD_W, maxWidth: 'calc(100% - 8px)' }}
    >
      <div className="flex items-baseline gap-2 flex-wrap mb-1.5 text-muted">
        <span className="font-bold" style={{ color: C.go }}>{label}</span>
        <span className="text-[10px] border rounded-sm px-1.5" style={{ color: tagColor, borderColor: tagColor }}>
          {i18nT(CARD_TAG_KEY[lane.cls])}
        </span>
        {lane.reopens > 0 && (
          <span className="text-[10px] border rounded-sm px-1.5" style={{ color: C.hold, borderColor: C.hold }}>
            {i18nT('apps.autoTriagePipeline.pipeline.card_reopen', { count: lane.reopens })}
          </span>
        )}
      </div>
      {lane.title ? (
        <div className="text-text-strong leading-snug mb-2">{lane.title}</div>
      ) : null}
      {lane.next && (
        <div className="text-[10px] text-muted leading-snug mb-2 -mt-1">
          <span className="text-muted-strong">{i18nT('apps.autoTriagePipeline.pipeline.card_next')}</span>{' '}
          {lane.next}
        </div>
      )}

      <table className="w-full border-collapse text-[10px]">
        <thead>
          <tr className="text-muted-strong">
            <td className="text-left pb-0.5">{i18nT('apps.autoTriagePipeline.pipeline.card_table_phase')}</td>
            <td className="text-left pb-0.5">{i18nT('apps.autoTriagePipeline.pipeline.card_table_at')}</td>
            <td className="text-right pb-0.5">{i18nT('apps.autoTriagePipeline.pipeline.card_table_dwell')}</td>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, k) => (
            <tr key={k}>
              <td className="py-[1.5px] text-text w-[52%]">{r.phase}</td>
              <td className="py-[1.5px] text-muted tabular-nums">{hhmm(r.atMs)}</td>
              <td className="py-[1.5px] text-right tabular-nums" style={{ color: r.slow ? C.hold : C.etch }}>
                {r.dwellSeconds != null ? i18nT('apps.autoTriagePipeline.pipeline.dwell_open', { dwell: dwellText(formatDwell(r.dwellSeconds)) }) : ''}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {openDwell != null && lane.cls !== 'done' && (
        <div className="mt-1.5 pt-1.5 border-t border-border text-[10px]" style={{ color: openDwell > 3600 ? C.hold : C.etch }}>
          {`${i18nT('apps.autoTriagePipeline.pipeline.and_counting', { dwell: i18nT('apps.autoTriagePipeline.pipeline.dwell_open', { dwell: dwellText(formatDwell(openDwell)) }) })} · ${lane.phase}`}
        </div>
      )}
    </div>
  )
}

/** NOTES + the colour legend, folded into the board card right under the drawing
 * they explain (defects #3 rows render, #18 no blank band, #21 legend near the
 * drawing). */
function NotesPanel() {
  const notes = [
    i18nT('apps.autoTriagePipeline.pipeline.note_1'),
    i18nT('apps.autoTriagePipeline.pipeline.note_2'),
    i18nT('apps.autoTriagePipeline.pipeline.note_3'),
    i18nT('apps.autoTriagePipeline.pipeline.note_4'),
    i18nT('apps.autoTriagePipeline.pipeline.note_5'),
  ]
  const legend: Array<{ color: string; label: string }> = [
    { color: C.go, label: i18nT('apps.autoTriagePipeline.pipeline.legend_editing') },
    { color: C.hold, label: i18nT('apps.autoTriagePipeline.pipeline.legend_waiting') },
    { color: C.etch, label: i18nT('apps.autoTriagePipeline.pipeline.legend_released') },
    { color: C.etch, label: i18nT('apps.autoTriagePipeline.pipeline.legend_done') },
  ]
  return (
    <div className="px-3 md:px-4 py-3 border-t border-border grid gap-3 md:grid-cols-[1fr_auto]">
      <div>
        <div className="text-[10px] tracking-[.14em] text-muted-strong uppercase mb-1.5">
          {i18nT('apps.autoTriagePipeline.pipeline.notes_heading')}
        </div>
        <ol className="flex flex-col gap-1">
          {notes.map((n, i) => (
            <li key={i} className="flex items-start gap-2 text-[11px] text-muted leading-snug">
              <span className="tabular-nums text-muted-strong flex-shrink-0 w-4">{`${i + 1}.`}</span>
              <span>{n}</span>
            </li>
          ))}
        </ol>
      </div>
      <div className="flex flex-col gap-1.5 md:items-end">
        <div className="text-[10px] tracking-[.14em] text-muted-strong uppercase mb-0.5">
          {i18nT('apps.autoTriagePipeline.pipeline.legend_heading')}
        </div>
        {legend.map((it) => (
          <span key={it.label} className="inline-flex items-center gap-1.5 text-[11px] text-muted">
            <span className="inline-block w-3 h-1 rounded-sm" style={{ backgroundColor: it.color }} />
            {it.label}
          </span>
        ))}
        <span className="text-[10px] text-muted opacity-60 mt-0.5">{i18nT('apps.autoTriagePipeline.pipeline.legend_hint')}</span>
      </div>
    </div>
  )
}

// ── a skeleton for the board while the first fabric loads ──

function LaneBoardSkeleton() {
  return (
    <div className="flex flex-col gap-2 py-2" aria-hidden="true">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="flex items-center gap-3">
          <div className="rounded-lg bg-bg-hover animate-pulse" style={{ width: ID_CARD_W, height: 44 }} />
          <div className="flex-1 h-2 rounded bg-bg-hover animate-pulse" />
        </div>
      ))}
    </div>
  )
}

// ── the designed empty state (the COMMON case: most installs never ran a crew) ──

function EmptyState() {
  const rows = [
    i18nT('apps.autoTriagePipeline.pipeline.empty_row_1'),
    i18nT('apps.autoTriagePipeline.pipeline.empty_row_2'),
    i18nT('apps.autoTriagePipeline.pipeline.empty_row_3'),
  ]
  return (
    <Card className="mb-0">
      <UIEmptyState
        icon={<Activity />}
        title={i18nT('apps.autoTriagePipeline.pipeline.empty_title')}
        subtitle={i18nT('apps.autoTriagePipeline.pipeline.empty_body')}
        testId="atp-empty"
      />
      <div className="flex flex-col gap-1.5 text-[12px] text-muted mx-auto max-w-md">
        {rows.map((r, i) => (
          <div key={i} className="flex items-start gap-2">
            <span className="text-accent flex-shrink-0 mt-0.5">▸</span>
            <span>{r}</span>
          </div>
        ))}
        <div className="text-[11px] text-muted opacity-60 mt-1 text-center">
          {i18nT('apps.autoTriagePipeline.pipeline.empty_footer')}
        </div>
      </div>
    </Card>
  )
}

// ── the repo picker: switch repo without leaving the app ──

function RepoPicker({
  repo, connected, onPick,
}: {
  repo: RepoRef
  connected: ConnectedRepo[]
  onPick: (ref: RepoRef) => void
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  const ordered = useMemo(
    () => [...connected].filter((r) => r && r.owner && r.repo)
      .sort((a, b) => byteOrder(repoOrderKey(a), repoOrderKey(b))),
    [connected],
  )
  const multiple = ordered.length > 1
  const label = `${repo.owner}/${repo.repo}`
  const activeProvider = repo.provider ?? 'github'
  const activeHost = repo.host ?? 'github.com'

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const isActive = (r: ConnectedRepo) =>
    r.owner === repo.owner && r.repo === repo.repo &&
    (r.provider ?? 'github') === activeProvider && (r.host ?? 'github.com') === activeHost

  return (
    <div ref={wrapRef} className="relative inline-block">
      <button
        type="button"
        onClick={() => multiple && setOpen((v) => !v)}
        disabled={!multiple}
        aria-haspopup={multiple ? 'listbox' : undefined}
        aria-expanded={multiple ? open : undefined}
        aria-label={i18nT('apps.autoTriagePipeline.pipeline.repo_picker_label')}
        title={multiple ? i18nT('apps.autoTriagePipeline.pipeline.repo_picker_label') : label}
        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 text-[12px] text-text-strong hover:border-border-strong disabled:cursor-default enabled:cursor-pointer font-mono"
      >
        <span className="text-[10px] uppercase tracking-[.14em] text-muted-strong">
          {i18nT('apps.autoTriagePipeline.pipeline.repo_picker_prefix')}
        </span>
        <span className="truncate max-w-[220px]">{label}</span>
        {multiple && <ChevronDown size={12} className="text-muted flex-shrink-0" />}
      </button>

      {open && multiple && (
        <div
          role="listbox"
          aria-label={i18nT('apps.autoTriagePipeline.pipeline.repo_picker_label')}
          className="absolute right-0 top-full mt-1 z-50 min-w-[240px] max-w-[360px] rounded-md border border-border-strong bg-card shadow-lg py-1 max-h-[60vh] overflow-y-auto"
        >
          {ordered.map((r) => {
            const active = isActive(r)
            const rlabel = `${r.owner}/${r.repo}`
            const sub = `${r.provider ?? 'github'} · ${r.host ?? 'github.com'}`
            return (
              <button
                key={repoOrderKey(r)}
                type="button"
                role="option"
                aria-selected={active}
                onClick={() => {
                  setOpen(false)
                  if (!active) {
                    onPick({ owner: r.owner, repo: r.repo, provider: r.provider, host: r.host })
                  }
                }}
                className="w-full flex items-center gap-2 px-2.5 py-1.5 text-left text-[11px] hover:bg-bg-elevated cursor-pointer font-mono"
              >
                <Check size={12} className={active ? 'text-accent flex-shrink-0' : 'opacity-0 flex-shrink-0'} />
                <span className="flex flex-col min-w-0">
                  <span className="truncate text-text-strong">{rlabel}</span>
                  <span className="truncate text-[10px] text-muted-strong uppercase tracking-[.06em]">{sub}</span>
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── a light holding state while the connected-repo list is still loading ──

function ResolvingState() {
  return (
    <div className="h-full overflow-y-auto pb-6">
      <PageChrome />
      <div className="px-4 md:px-6">
        <Card><LaneBoardSkeleton /></Card>
      </div>
    </div>
  )
}

// ── shown when NO repo is connected anywhere — the genuine empty state ──

function NoRepoState() {
  return (
    <div className="h-full overflow-y-auto pb-6">
      <PageChrome />
      <div className="px-4 md:px-6">
        <Card className="mb-0">
          <UIEmptyState
            icon={<Activity />}
            title={i18nT('apps.autoTriagePipeline.pipeline.no_repo_title')}
            subtitle={i18nT('apps.autoTriagePipeline.pipeline.no_repo_body')}
            testId="atp-no-repo"
          />
        </Card>
      </div>
    </div>
  )
}
