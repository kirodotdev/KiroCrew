/**
 * The first-share diagram on the MCP page. Two rounded panels: "Off", three
 * chats each running its own copy of a server, and "On", three chats that use
 * one copy. The On panel plays once when shown: each chat starts with its own
 * server, the three servers move down and merge into one shared server, and
 * thin curved lines draw into it. Timing, easing and the purple-to-blue light
 * are the conductor mode turn-on effect's (`components/crew/ConductorModeEffect`).
 * It stops when done; a new `run` replays it. Under prefers-reduced-motion it
 * is the still Off/On pair.
 *
 * Built from lucide icons and positioned HTML boxes (no inline SVG, per the
 * `use-lucide-icons` rule): positions are percentages of a 170 x 100 drawing
 * box, so the picture scales with the panel. Theme variables only; the figure
 * carries one label for assistive tech.
 */
import type { CSSProperties, ReactNode } from 'react'
import { motion } from 'framer-motion'
import { MessageSquare, Server } from 'lucide-react'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { i18nT } from '../../i18n/t'

/** How long one play lasts, in ms (the last part ends here). */
export const SHARE_DIAGRAM_PLAY_MS = 1600

/** The conductor effect's sweep easing, used for the move. */
const EASE_MOVE = [0.4, 0, 0.2, 1] as const
const MOVE = { delay: 0.3, duration: 0.75, ease: EASE_MOVE }
/** Where the count label swaps, within the move: a short cross-fade at its middle. */
const SWAP_TIMES = [0, 0.4, 0.6, 1]
/** The saved pill: the merge's easing, starting late so it lands last. */
const SAVED = { delay: 0.8, duration: 0.75, ease: EASE_MOVE }
const LINES = { delay: 0.75, duration: 0.7, ease: 'easeOut' as const }
const ARRIVE = { delay: 0.95, duration: 0.6, ease: 'easeOut' as const, times: [0, 0.35, 1] }
/** The same moment for a value with one target: `times` needs one entry per keyframe. */
const FADE = { delay: 0.95, duration: 0.6, ease: 'easeOut' as const }

/** The drawing box, in the units every position below is written in. */
const BOX_W = 170
const BOX_H = 100
const BUBBLE = 40
const BUBBLE_XS = [30, 85, 140]
const TOP_ON = 4
const TOP_OFF = 18
/** Where a server sits inside a bubble, from the bubble's top (the body, above the tail). */
const GLYPH_DY = 17
const GLYPH = 16
const HUB = { x: 85, y: 82 }
const HALO = 32

/** A point in the art box as `left`/`top` percentages, and a width or height as a share of it. */
const at = (x: number, y: number) => ({ left: `${(x / BOX_W) * 100}%`, top: `${(y / BOX_H) * 100}%` })
const wide = (w: number) => ({ width: `${(w / BOX_W) * 100}%` })
const tall = (h: number) => ({ height: `${(h / BOX_H) * 100}%` })
/** Centres a box on its left/top point; framer composes this with its own transforms. */
const CENTRED = { x: '-50%', y: '-50%' }

function Bubbles({ top }: { top: number }) {
  return (
    <>
      {BUBBLE_XS.map(x => (
        <span
          key={x}
          className="absolute aspect-square -translate-x-1/2"
          style={{ ...at(x, top), ...wide(BUBBLE) }}
        >
          <MessageSquare
            className="h-full w-full text-[var(--border-strong,var(--border))]"
            fill="var(--card)"
            strokeWidth={1.5}
          />
        </span>
      ))}
    </>
  )
}

function Glyph({ colour }: { colour: string }) {
  return <Server className="h-full w-full" style={{ color: colour }} strokeWidth={2} />
}

function ArtBox({ children }: { children: ReactNode }) {
  return (
    <div aria-hidden="true" className="relative w-full" style={{ aspectRatio: `${BOX_W} / ${BOX_H}` }}>
      {children}
    </div>
  )
}

function OffArt() {
  return (
    <ArtBox>
      <Bubbles top={TOP_OFF} />
      {BUBBLE_XS.map(x => (
        <span
          key={x}
          className="absolute aspect-square -translate-x-1/2 -translate-y-1/2"
          style={{ ...at(x, TOP_OFF + GLYPH_DY), ...wide(GLYPH) }}
        >
          <Glyph colour="var(--muted)" />
        </span>
      ))}
    </ArtBox>
  )
}

/** One connector from a bubble's foot to the hub, drawn top-down. */
function Connector({ x, still }: { x: number; still: boolean }) {
  const top = TOP_ON + BUBBLE - 4
  const side = x < HUB.x ? 'left' : x > HUB.x ? 'right' : 'middle'
  const style: CSSProperties =
    side === 'middle'
      ? { ...at(x, top), ...tall(HUB.y - top), width: 1 }
      : {
          ...at(Math.min(x, HUB.x), top),
          ...tall(HUB.y - top),
          ...wide(Math.abs(HUB.x - x)),
          borderBottomWidth: 1,
          [side === 'left' ? 'borderLeftWidth' : 'borderRightWidth']: 1,
          [side === 'left' ? 'borderBottomLeftRadius' : 'borderBottomRightRadius']: '100% 100%',
        }
  return (
    <motion.span
      data-testid="share-diagram-line"
      className={
        side === 'middle'
          ? 'absolute bg-[color-mix(in_srgb,var(--aim)_55%,var(--info))]'
          : 'absolute border-[color-mix(in_srgb,var(--aim)_55%,var(--info))]'
      }
      style={style}
      initial={still ? false : { clipPath: 'inset(0 0 100% 0)' }}
      animate={{ clipPath: 'inset(0 0 0% 0)' }}
      transition={LINES}
    />
  )
}

/** The On panel's drawing. `play` false draws the settled state. */
function OnArt({ play }: { play: boolean }) {
  const still = !play
  return (
    <>
      <ArtBox>
        {BUBBLE_XS.map(x => (
          <Connector key={x} x={x} still={still} />
        ))}
        <motion.span
          data-testid="share-diagram-halo"
          className="absolute aspect-square rounded-full"
          style={{
            ...CENTRED,
            ...at(HUB.x, HUB.y),
            ...wide(HALO),
            background:
              'radial-gradient(circle, color-mix(in srgb, var(--aim) 32%, transparent) 0%, color-mix(in srgb, var(--info) 14%, transparent) 60%, transparent 100%)',
          }}
          initial={still ? false : { scale: 0.3, opacity: 0 }}
          animate={still ? { scale: 1, opacity: 1 } : { scale: [0.3, 1.6, 1], opacity: [0, 1, 1] }}
          transition={ARRIVE}
        />
        <Bubbles top={TOP_ON} />
        {/* Each chat's own server travels down to the hub and fades as the shared one lights. */}
        {play &&
          BUBBLE_XS.map(x => (
            <motion.span
              key={x}
              data-testid="share-diagram-own-server"
              className="absolute aspect-square"
              style={{ ...CENTRED, ...wide(GLYPH) }}
              initial={{ ...at(x, TOP_ON + GLYPH_DY), opacity: 1 }}
              animate={{ ...at(HUB.x, HUB.y), opacity: [1, 1, 0] }}
              transition={{ ...MOVE, opacity: { ...MOVE, times: [0, 0.85, 1] } }}
            >
              <Glyph colour="var(--muted)" />
            </motion.span>
          ))}
        <motion.span
          className="absolute aspect-square"
          style={{ ...CENTRED, ...at(HUB.x, HUB.y), ...wide(GLYPH) }}
          initial={still ? false : { opacity: 0, scale: 0.6 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={FADE}
        >
          <Glyph colour="var(--aim)" />
        </motion.span>
      </ArtBox>
      {/* Under the shared server, which sits at the drawing's centre line. */}
      <motion.span
        className="-mt-1 block text-[11px] text-[var(--muted)]"
        initial={still ? false : { opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={FADE}
      >
        {i18nT('pages.mcpManagement.share_diagram_mcp_server')}
      </motion.span>
    </>
  )
}

/** What the two glyphs stand for, under both panels. */
function Legend() {
  const item = 'inline-flex items-center gap-1.5'
  return (
    <div className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[11.5px] text-[var(--muted)]">
      <span className={item}>
        <MessageSquare size={15} strokeWidth={1.75} aria-hidden="true" />
        {i18nT('pages.mcpManagement.share_diagram_legend_chat')}
      </span>
      <span className={item}>
        <Server size={15} strokeWidth={1.75} aria-hidden="true" />
        {i18nT('pages.mcpManagement.share_diagram_mcp_server')}
      </span>
    </div>
  )
}

/**
 * How many copies of the server run: one segment per copy. `freed` segments
 * stay as a dashed outline. With `play`, the freed segments start filled and
 * empty out with the merge (same easing and 0.75 s), and the count label
 * changes with them.
 */
function MemoryBar({ copies, freed, play }: { copies: number; freed: number; play: boolean }) {
  const before = i18nT('pages.mcpManagement.share_diagram_copies', { count: copies + freed })
  const after = i18nT('pages.mcpManagement.share_diagram_copies', { count: copies })
  return (
    <div
      data-testid={freed ? 'share-diagram-bar-on' : 'share-diagram-bar-off'}
      className="mt-1.5 flex min-w-0 flex-col items-center gap-1.5"
    >
      {/* As wide as the three chat bubbles above it. */}
      <div aria-hidden="true" className="grid w-[88%] grid-cols-3 gap-1">
        {Array.from({ length: copies + freed }, (_, i) => {
          const isFreed = i >= copies
          return (
            <span
              key={i}
              data-segment={isFreed ? 'freed' : 'filled'}
              data-start={isFreed && play ? 'filled' : isFreed ? 'freed' : 'filled'}
              data-outline={isFreed ? 'dashed' : undefined}
              className={[
                'relative h-2 overflow-hidden rounded-full',
                isFreed
                  ? 'border border-dashed'
                  : freed
                    ? 'bg-[var(--accent)]'
                    : 'bg-[color-mix(in_srgb,var(--text)_72%,transparent)]',
              ].join(' ')}
              style={isFreed ? { borderColor: 'color-mix(in srgb, var(--accent) 55%, transparent)' } : undefined}
            >
              {isFreed && play && (
                <motion.span
                  className="absolute inset-0 rounded-full bg-[var(--accent)]"
                  initial={{ opacity: 1 }}
                  animate={{ opacity: 0 }}
                  transition={MOVE}
                />
              )}
            </span>
          )
        })}
      </div>
      <span className="relative block max-w-full text-[12.5px] text-[var(--text)]">
        {/* While the merge plays, the count swaps at the middle of the move. */}
        {freed > 0 && play && (
          <motion.span
            aria-hidden="true"
            className="absolute inset-y-0 left-0 whitespace-nowrap"
            initial={{ opacity: 1 }}
            animate={{ opacity: [1, 1, 0, 0] }}
            transition={{ ...MOVE, times: SWAP_TIMES }}
          >
            {before}
          </motion.span>
        )}
        {/* Wraps rather than clipping when a narrow panel cannot hold the count and the pill. */}
        <motion.span
          className="flex flex-wrap items-center justify-center gap-x-1.5 gap-y-1"
          initial={freed > 0 && play ? { opacity: 0 } : false}
          animate={freed > 0 && play ? { opacity: [0, 0, 1, 1] } : { opacity: 1 }}
          transition={{ ...MOVE, times: SWAP_TIMES }}
        >
          <span>{after}</span>
          {freed > 0 && (
            <motion.span
              data-testid="share-diagram-saved"
              className="rounded-full bg-[var(--accent-subtle,var(--hover))] px-1.5 py-px text-[11px] font-medium text-[var(--accent)]"
              initial={play ? { opacity: 0, scale: 0.8 } : false}
              animate={{ opacity: 1, scale: 1 }}
              transition={SAVED}
            >
              {i18nT('pages.mcpManagement.share_diagram_saved')}
            </motion.span>
          )}
        </motion.span>
      </span>
    </div>
  )
}

function PanelShell({
  label,
  caption,
  tint,
  children,
}: {
  label: string
  caption: string
  tint: 'none' | 'still' | 'play'
  children: ReactNode
}) {
  return (
    <div className="relative min-w-0 overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg)] px-3 pb-3 pt-2.5 text-center">
      {tint !== 'none' && (
        <motion.div
          aria-hidden="true"
          // Accent mixed into the page background, not --accent-subtle, which
          // is close to invisible on dark themes.
          className="absolute inset-0 rounded-xl border"
          style={{
            backgroundColor: 'color-mix(in srgb, var(--accent) 16%, var(--bg))',
            borderColor: 'color-mix(in srgb, var(--accent) 45%, transparent)',
          }}
          initial={tint === 'play' ? { opacity: 0 } : false}
          animate={{ opacity: 1 }}
          transition={FADE}
        />
      )}
      <div className="relative">
        <span className="mb-1.5 block text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
          {label}
        </span>
        {children}
        <span className="mt-1 block text-[12.5px] text-[var(--muted)]">{caption}</span>
      </div>
    </div>
  )
}

/**
 * `run` is a play counter: each new value replays the On panel from the Off
 * state. Under reduced motion every value draws the same still pair.
 */
export function McpShareDiagram({ id, run = 0 }: { id?: string; run?: number }) {
  const play = !useReducedMotion()
  return (
    <figure
      id={id}
      role="img"
      aria-label={i18nT('pages.mcpManagement.share_diagram_label')}
      data-motion={play ? 'play' : 'still'}
      data-run={run}
      className="@container m-0 min-w-0 space-y-2"
    >
      {/* One column in a narrow pane; side by side once both panels fit. */}
      <div className="grid min-w-0 grid-cols-1 gap-3 @[18rem]:grid-cols-2">
        <PanelShell
          label={i18nT('pages.mcpManagement.share_diagram_state_off')}
          caption={i18nT('pages.mcpManagement.share_diagram_off')}
          tint="none"
        >
          <OffArt />
          <span aria-hidden="true" className="-mt-1 block text-[11px]">&nbsp;</span>
          <MemoryBar copies={3} freed={0} play={false} />
        </PanelShell>
        <PanelShell
          key={run}
          label={i18nT('pages.mcpManagement.share_diagram_state_on')}
          caption={i18nT('pages.mcpManagement.share_diagram_on')}
          tint={play ? 'play' : 'still'}
        >
          <OnArt play={play} />
          <MemoryBar copies={1} freed={2} play={play} />
        </PanelShell>
      </div>
      <Legend />
    </figure>
  )
}
