/**
 * The crew overview: what wakes this crew on the left, what it works with on the
 * right, the crew itself in the middle.
 *
 * The two columns exist because the editor's hardest copy problem is that
 * `triggers` (which decides when the orchestrator PICKS this crew for work a
 * human started) and a schedule (which starts a turn with nobody present) read as
 * the same kind of thing when stacked as prose — `CrewWakeSection` carries a
 * sentence of disclaimer to say they are not. Direction is the thing being
 * explained, so it is drawn rather than asserted.
 *
 * Connectors are COMPUTED from the two columns' lengths, never hand-placed. That
 * is what makes a new binding free: adding a node re-fans the connectors with no
 * edit here, whereas a diagram with baked coordinates has to be redrawn each time
 * and is why hand-drawn diagrams rot.
 *
 * They are drawn as rounded elbows built from element BORDERS rather than SVG
 * paths. `use-lucide-icons` (website/AUTOSDE.yaml) blocks inline SVG elements in
 * any `.tsx`, and its exemption only covers art that stays an `.svg` asset
 * rendered through an image tag — which a fan whose geometry depends on runtime
 * list lengths cannot be. Borders also inherit the palette through `var(--…)`, so
 * the elbows re-theme with everything else instead of freezing a colour into a
 * data URI. (This note avoids spelling the blocked pattern: the gate matches the
 * text of added lines, comments included.)
 */
import type { LucideIcon } from 'lucide-react'

/** One box in either column. */
export interface CrewWireNode {
  key: string
  icon: LucideIcon
  /** What kind of thing it is, e.g. "Workspace". */
  label: string
  /** What it currently points at, e.g. `oncall`. */
  value: string
  /** Render the value in the mono face — for identifiers the user can copy. */
  mono?: boolean
  /** Render the value italic and muted — for "Inherited", which is an absence. */
  muted?: boolean
  /** Short status pill on the row's trailing edge. */
  tag?: string
  /** Dashed and dimmed: a real input that carries no crew binding yet. Drawn so
   *  the gap is visible rather than absent. */
  ghost?: boolean
}

/** Row pitch in px. One node box plus its gap; the fan maths depends on it. */
const ROW = 48
/** Node box height. The boxes are a fixed height so a curve meets its box
 *  centre — a box that grew with its content would drift off its connector. */
const BOX = ROW - 7

/** Vertical centre of node `i` inside a column of `n`, within a band of height
 *  `h`. Both columns are centred in the same band, so a 3-node column and a
 *  4-node one still meet the hub at the right heights. */
function centreOf(i: number, n: number, h: number): number {
  return (h - n * ROW) / 2 + i * ROW + ROW / 2
}

/**
 * One rounded elbow, from a node's centre to the hub's centre (or the reverse).
 *
 * Two borders on one box: the horizontal run leaves the node, the vertical run
 * meets the hub, and the corner between them is rounded. A node level with the
 * hub degenerates to a straight rule, which is why the zero-height case is
 * handled rather than rounded.
 */
function Elbow({ from, to, width, colour, dashed, toHub }: {
  from: number
  to: number
  width: number
  colour: string
  dashed?: boolean
  /** True when the run ends at the hub, so the vertical leg sits on the far
   *  edge; false when it starts there and the leg sits on the near edge. */
  toHub: boolean
}) {
  const top = Math.min(from, to)
  const height = Math.abs(from - to)
  const style: React.CSSProperties = {
    position: 'absolute',
    left: 0,
    top,
    width,
    height,
    borderColor: colour,
    borderStyle: dashed ? 'dashed' : 'solid',
    opacity: dashed ? 0.5 : 0.85,
  }
  if (height === 0) {
    return <span aria-hidden="true" data-connector style={{ ...style, borderTopWidth: 1.2 }} />
  }
  // The horizontal leg belongs on the NODE end of the run, and the vertical leg
  // on the hub end. Choosing the leg from the run's direction instead gets the
  // outbound fan backwards: its elbows collapse onto one trunk with no arm
  // reaching any node.
  const nodeY = toHub ? from : to
  const legOnTop = nodeY === top
  const vertical = toHub ? 'borderRightWidth' : 'borderLeftWidth'
  const horizontal = legOnTop ? 'borderTopWidth' : 'borderBottomWidth'
  // Spelled out rather than assembled from fragments: a composed property name is
  // unsearchable, and the fragments read as prose to the untranslated-literal gate.
  const radius = legOnTop
    ? (toHub ? 'borderTopRightRadius' : 'borderTopLeftRadius')
    : (toHub ? 'borderBottomRightRadius' : 'borderBottomLeftRadius')
  return (
    <span
      aria-hidden="true"
      data-connector
      data-node-edge={legOnTop ? 'top' : 'bottom'}
      style={{
        ...style,
        [vertical]: 1.2,
        [horizontal]: 1.2,
        [radius]: Math.min(10, height / 2, width / 2),
      }}
    />
  )
}

/** A whole fan: one elbow per node, positioned from the column's LENGTH. */
function Fan({ nodes, width, band, colour, toHub }: {
  nodes: CrewWireNode[]
  width: number
  band: number
  colour: string
  toHub: boolean
}) {
  const mid = band / 2
  return (
    <div
      className="relative hidden shrink-0 sm:block"
      style={{ width, height: band }}
      data-testid={`crew-wire-fan-${toHub ? 'in' : 'out'}`}
      aria-hidden="true"
    >
      {nodes.map((n, i) => {
        const y = centreOf(i, nodes.length, band)
        return (
          <Elbow
            key={n.key}
            from={toHub ? y : mid}
            to={toHub ? mid : y}
            width={width}
            colour={colour}
            dashed={n.ghost}
            toHub={toHub}
          />
        )
      })}
    </div>
  )
}

function WireNode({ node, side, onSelect }: {
  node: CrewWireNode
  side: 'in' | 'out'
  /** Selecting the node opens the pane that edits what it shows. Required:
   *  a box that looks pressable but answers nothing is the affordance bug
   *  this component exists to prevent, so an inert render is unrepresentable. */
  onSelect: () => void
}) {
  const Icon = node.icon
  const accent = side === 'in' ? 'border-aim/60' : 'border-accent/45'
  const chip = side === 'in' ? 'bg-aim-subtle text-aim' : 'bg-accent-subtle text-accent'
  const shell = [
    'flex items-center gap-2 rounded-lg border bg-bg-elevated px-2.5 py-1.5',
    node.ghost ? 'border-dashed border-border-strong opacity-60' : accent,
  ]
  const inner = (
    <>
      <span
        className={[
          'flex h-[23px] w-[23px] shrink-0 items-center justify-center rounded-md',
          node.ghost ? 'bg-bg-hover text-muted' : chip,
        ].join(' ')}
      >
        <Icon className="lucide-inline h-[13px] w-[13px]" aria-hidden="true" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[10px] uppercase tracking-[0.07em] leading-tight text-muted">
          {node.label}
        </span>
        {/* `pr-0.5` with `truncate`: an italic glyph leans past its own advance
            width and `overflow:hidden` clips the overhang instead of eliding. */}
        <span
          className={[
            'block truncate pr-0.5 text-[12px] leading-tight',
            node.muted ? 'italic text-muted' : 'text-text-strong',
            node.mono && !node.muted ? 'font-mono' : '',
          ].join(' ')}
        >
          {node.value}
        </span>
      </span>
      {node.tag && (
        <span className="shrink-0 rounded border border-info bg-info-subtle px-1.5 text-[10px] text-info">
          {node.tag}
        </span>
      )}
    </>
  )
  // The button's accessible name is its own text — the label already says what
  // the pane it opens edits, so a separate string would restate it. A ghost
  // node stays selectable: its pane is where the missing binding gets made.
  return (
    <button
      type="button"
      onClick={onSelect}
      className={[
        ...shell,
        'w-full text-left focus-ring',
        // An interactive control must not stay at 60% under the pointer or
        // focus: the ghost's dimming is a reading of absence, and it yields to
        // full contrast the moment the control is being operated. The ghost's
        // hover background also differs from the solid nodes' because its icon
        // chip is itself `bg-bg-hover` — the same token on the whole button
        // would melt the chip into it.
        node.ghost
          ? 'hover:bg-bg-accent hover:opacity-100 focus-visible:opacity-100'
          : 'hover:bg-bg-hover',
      ].join(' ')}
      style={{ height: BOX }}
      data-testid={`crew-wire-${node.key}`}
    >
      {inner}
    </button>
  )
}

export interface CrewOverviewDiagramProps {
  inputs: CrewWireNode[]
  outputs: CrewWireNode[]
  /** Heading over the left column. */
  inputsLabel: string
  /** Heading over the right column. */
  outputsLabel: string
  /** The crew's avatar, rendered in the hub. */
  hub: React.ReactNode
  /** Called with the node's `key` when a node is selected. Every node renders
   *  as a button; the map from key to editor pane lives with the caller. */
  onNodeSelect: (key: string) => void
}

export default function CrewOverviewDiagram({
  inputs, outputs, inputsLabel, outputsLabel, hub, onNodeSelect,
}: CrewOverviewDiagramProps) {
  const band = Math.max(inputs.length, outputs.length, 1) * ROW

  const column = (nodes: CrewWireNode[], side: 'in' | 'out', label: string) => (
    <div className="min-w-0 flex-1">
      <div className="mb-1.5 text-[10px] uppercase tracking-[0.08em] text-muted-strong">{label}</div>
      <div className="flex flex-col" style={{ gap: ROW - BOX }}>
        {nodes.map(n => (
          <WireNode key={n.key} node={n} side={side} onSelect={() => onNodeSelect(n.key)} />
        ))}
      </div>
    </div>
  )

  return (
    // Narrow-first: below `sm` the two columns stack and the connectors are
    // dropped, because an elbow between vertically stacked boxes states a
    // direction the layout no longer has.
    <div
      className="flex flex-col gap-4 sm:flex-row sm:items-center sm:gap-0"
      data-testid="crew-overview-diagram"
    >
      {column(inputs, 'in', inputsLabel)}
      <Fan nodes={inputs} width={56} band={band} colour="var(--aim)" toHub />
      <div className="mx-auto flex h-16 w-16 shrink-0 items-center justify-center rounded-full
                      border border-accent bg-bg-elevated">
        {hub}
      </div>
      <Fan nodes={outputs} width={36} band={band} colour="var(--accent)" toHub={false} />
      {column(outputs, 'out', outputsLabel)}
    </div>
  )
}
