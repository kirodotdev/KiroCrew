import { memo, useState, useEffect, useMemo, useRef, type ComponentType } from 'react'
import { Hourglass, Search, Lightbulb, Settings, Zap, Check, Sparkles, Brain, Pen } from 'lucide-react'
import { motion } from 'framer-motion'

import { i18nT } from '../../i18n/t'
import { useI18nRevision } from '../../i18n/useI18nRevision'
import { GHOST_POSE_ICONS } from '../../components/GhostPoses'
import { getThemeBranding } from '../../themeBranding'
import ErrorBoundary from '../../components/ErrorBoundary'
type StopState = 'idle' | 'soft_pending' | 'killing'

/** One carousel beat — the cadence the .csb4 cross-fade was built around. */
const CYCLE_MS = 2800
const SLOTS = 4

// Each slot stacks two layers that trade places over one cycle (csb-a / csb-b),
// and slots are staggered .25s apart. A layer may only be re-sampled while it is
// HIDDEN in every slot, or the swap would pop mid-fade. Derived from the keyframes
// plus the 0.75s worst-case stagger:
//   layer A (csb-a) is hidden in all slots ~1.81s–2.30s  -> swap at 2.00s
//   layer B (csb-b) is hidden in all slots ~0.47s–1.06s  -> swap at 0.80s
const SWAP_A_MS = 2000
const SWAP_B_MS = 800

/** The default icon pool, used by every theme that registers no artwork. */
const DEFAULT_ICONS: ComponentType[] = [Search, Lightbulb, Settings, Zap, Check, Sparkles, Brain, Pen]

/** Bundled artwork for themes the core ships. A theme registered through the
 *  `themeBranding` seam takes precedence over anything here. */
const BUNDLED_THEME_ICONS: Record<string, ComponentType[]> = {
  kiro: GHOST_POSE_ICONS,
}

/** Sample `SLOTS` DISTINCT indices out of `total`, avoiding any of the given sets
 *  so a swap always produces a visibly different group (never a repeat of what it
 *  replaces, and never a duplicate of the other layer). */
export function pickDistinct(total: number, ...avoid: (readonly number[] | undefined)[]): number[] {
  const n = Math.min(SLOTS, total)
  const same = (x: number[], y?: readonly number[]) =>
    !!y && y.length === x.length && x.every((v, i) => v === y[i])
  for (let attempt = 0; attempt < 12; attempt++) {
    const pool = Array.from({ length: total }, (_, i) => i)
    // Partial Fisher–Yates: draw `n` without replacement, so no duplicates.
    for (let i = 0; i < n; i++) {
      const j = i + Math.floor(Math.random() * (total - i))
      ;[pool[i], pool[j]] = [pool[j], pool[i]]
    }
    const next = pool.slice(0, n)
    if (!avoid.some(a => same(next, a))) return next
  }
  return Array.from({ length: n }, (_, i) => i)
}

const prefersReducedMotion = () =>
  typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

/**
 * The active colour-theme slug, tracked reactively.
 *
 * Read off `<html data-theme>` rather than `useTheme()` so the footer stays
 * usable without a ThemeProvider. `applyTheme` composes that attribute as
 * `<slug>-<mode>` (and bare `<mode>` for the base 'emerald' theme), so the mode
 * suffix is stripped back off to recover the slug the themeBranding registry is
 * keyed by. Mirrors the MonacoCodeBlock / CliPanel MutationObserver pattern, so
 * a mid-turn theme switch swaps the artwork without needing a remount.
 */
function useThemeSlug(): string {
  const read = () => {
    const t = document.documentElement.getAttribute('data-theme') || ''
    // Bare 'dark'/'light' is the base theme (applyTheme omits its slug).
    if (t === '' || t === 'dark' || t === 'light') return 'emerald'
    // Strip only a trailing '-dark'/'-light' SEGMENT. A loose /-?(dark|light)$/
    // would also eat the tail of a slug that merely ends in those letters
    // (e.g. 'highlight' -> 'high').
    return t.replace(/-(dark|light)$/, '')
  }
  const [slug, setSlug] = useState(read)
  useEffect(() => {
    const update = () => setSlug(read())
    update()
    const obs = new MutationObserver(update)
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
  }, [])
  return slug
}

/** Resolve what the footer should show while a turn runs. A theme may replace the
 *  whole loader, or just the artwork the default carousel cycles through:
 *    1. `loader`      — the theme's own component, rendered instead of everything
 *    2. `loaderIcons` — the stock carousel, cycling the theme's artwork
 *    3. artwork bundled for a core theme
 *    4. the default icons
 *  An empty registered pool is ignored rather than rendering nothing. */
export function resolveLoader(slug: string):
  | { kind: 'custom'; Component: ComponentType }
  | { kind: 'icons'; icons: ComponentType[] } {
  const branding = getThemeBranding(slug)
  if (branding?.loader) return { kind: 'custom', Component: branding.loader }
  return { kind: 'icons', icons: resolveLoaderIcons(slug) }
}

/** The icon pool for the default carousel under a given theme. */
export function resolveLoaderIcons(slug: string): ComponentType[] {
  const registered = getThemeBranding(slug)?.loaderIcons
  if (registered && registered.length > 0) return registered
  return BUNDLED_THEME_ICONS[slug] ?? DEFAULT_ICONS
}

/**
 * The 4-slot loading carousel: two layers per slot cross-fading via csb-a/csb-b,
 * cascading .25s apart. Every beat, each layer re-samples 4 distinct items from
 * the pool while it is off screen, so the icons keep changing instead of looping
 * the same eight forever.
 *
 * The animation lives on a persistent `.lyr` wrapper, NOT on the icon itself:
 * swapping an item changes the rendered component type, which remounts its <svg>
 * and would restart that element's animation, desyncing it from the other layer.
 * Animating the wrapper lets the artwork inside change freely.
 */
export function SwapCarousel({ icons }: { icons: ComponentType[] }) {
  const total = icons.length
  const reduce = useMemo(prefersReducedMotion, [])
  const [sets, setSets] = useState(() => {
    const a = pickDistinct(total)
    return { a, b: pickDistinct(total, a) }
  })

  // The swap timers read the pool size through a ref, so they are armed ONCE per
  // mount. Keying the timer effect on `total` would re-arm them on a theme switch
  // and re-phase the swaps against the CSS cross-fade — which never restarts,
  // because .lyr persists — landing a swap while a layer is visible.
  const totalRef = useRef(total)
  totalRef.current = total

  // Re-seed when the pool changes (theme switch) so the sets are distinct within
  // the NEW pool. Render is already index-safe (see the modulo below); this is
  // about keeping the 4-distinct guarantee, not about avoiding a crash.
  useEffect(() => {
    setSets(() => {
      const a = pickDistinct(total)
      return { a, b: pickDistinct(total, a) }
    })
  }, [total])

  useEffect(() => {
    if (reduce) return                       // hold one pair; no churn under reduced motion
    let intA = 0, intB = 0
    const swapA = () => setSets(s => ({ ...s, a: pickDistinct(totalRef.current, s.a, s.b) }))
    const swapB = () => setSets(s => ({ ...s, b: pickDistinct(totalRef.current, s.b, s.a) }))
    // Phase each layer's swap into its own hidden window, then hold the beat.
    const toA = window.setTimeout(() => { swapA(); intA = window.setInterval(swapA, CYCLE_MS) }, SWAP_A_MS)
    const toB = window.setTimeout(() => { swapB(); intB = window.setInterval(swapB, CYCLE_MS) }, SWAP_B_MS)
    return () => {
      window.clearTimeout(toA); window.clearTimeout(toB)
      window.clearInterval(intA); window.clearInterval(intB)
    }
  }, [reduce])

  // A theme switch changes the pool DURING render, while `sets` still holds the
  // previous pool's indices — the re-seed effect only runs afterwards. Two ways
  // that renders `<undefined />` (which throws "Element type is invalid" and takes
  // the footer down mid-turn), so guard both:
  //   pool SHRANK  — an index is now out of range        -> wrap it
  //   pool GREW    — the old sets are SHORTER than the   -> render only as many
  //                  slot count, so sets.a[3] is             slots as the sets can
  //                  undefined (and NaN once wrapped)        actually fill
  if (total === 0) return null
  const at = (i: number) => icons[i % total]
  const slotCount = Math.min(SLOTS, total, sets.a.length, sets.b.length)

  return (
    <div className="csb4" data-testid="loader-carousel">
      {Array.from({ length: slotCount }, (_, i) => {
        const A = at(sets.a[i])
        const B = at(sets.b[i])
        return (
          <div className="slot" key={i}>
            <span className="lyr" data-layer="a" data-item={sets.a[i] % total}><A /></span>
            <span className="lyr" data-layer="b" data-item={sets.b[i] % total}><B /></span>
          </div>
        )
      })}
    </div>
  )
}

/** How long the text stream must stay quiet before the footer takes over from the
 *  inline caret. Ordinary inter-chunk jitter is tens of milliseconds, so this is
 *  far above the noise floor while still short enough that the model's tool-call
 *  gap does not read as a stalled turn. */
export const STREAM_IDLE_MS = 700

/**
 * True once `tick` has not advanced for `ms` while `active` — i.e. the text stream
 * has gone quiet without the turn ending.
 *
 * One timer, re-armed on each tick: no polling interval, and nothing runs at all
 * once the stream is inactive.
 */
export function useStreamIdle(tick: number, active: boolean, ms: number = STREAM_IDLE_MS): boolean {
  const [idle, setIdle] = useState(false)
  useEffect(() => {
    if (!active) { setIdle(false); return }
    setIdle(false)
    const t = window.setTimeout(() => setIdle(true), ms)
    return () => window.clearTimeout(t)
  }, [tick, active, ms])
  return active && idle
}

const ChatFooter = memo(function ChatFooter({ running, stopping, state, lastRole, regenerating, stopState, streamTick = 0 }: { running: boolean; stopping: boolean; state: string; lastRole: string; regenerating?: boolean; stopState?: StopState; streamTick?: number }) {
  useI18nRevision()
  const loader = resolveLoader(useThemeSlug())
  // Text is only ACTIVELY streaming while the slot says so AND chunks keep
  // arriving. `lastRole` alone cannot tell the two apart: the trailing
  // 'streaming' message is deliberately left unfinalized across a whole tool
  // group (chat_segment is withheld so tool ordering survives), and the model
  // also goes quiet for seconds while it generates a tool call. Both looked like
  // "still streaming", so the loader stayed hidden with nothing else moving.
  const streamingText = lastRole === 'streaming' && state === 'streaming'
  const streamQuiet = useStreamIdle(streamTick, streamingText)
  // Hidden once the turn is inactive. While the turn RUNS the indicator shows for
  // thinking, tool calls, AND the gaps between steps — the post-tool gap used to
  // fall through every guard, so the indicator vanished mid-turn even though the
  // backend keeps slot.running true for the whole turn.
  if (!regenerating && !running && stopState !== 'soft_pending' && stopState !== 'killing') return null
  // ...but never while text is actively arriving: MarkdownRenderer already renders
  // the real blinking caret (.streaming-caret) there, and a second indicator
  // alongside it reads as two cursors.
  if (!regenerating && streamingText && !streamQuiet && stopState !== 'soft_pending' && stopState !== 'killing') return null
  // width from CSS var --mc-content-width
  return (
    <div data-testid="chat-footer" className={`px-5 mx-auto w-full py-1${regenerating ? '' : ' animate-slide-up'}`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="px-3.5 py-2.5">
        {stopState === 'soft_pending' ? (
          <motion.span
            className="text-danger text-[13px] font-mono"
            animate={{ opacity: [0.6, 1, 0.6] }}
            transition={{ duration: 1.2, repeat: Infinity }}
          >{i18nT('pages.chat.chatFooter.stopping')}</motion.span>
        ) : stopState === 'killing' ? (
          <span className="text-danger text-[13px] font-mono">{i18nT('pages.chat.chatFooter.killing')}</span>
        ) : !regenerating && stopping ? (
          <span className="text-muted text-[13px] font-mono animate-pulse">{i18nT('pages.chat.chatFooter.stopping')}</span>
        ) : !regenerating && state === 'compacting' ? (
          <span className="text-muted text-[13px] font-mono animate-pulse"><Hourglass className="lucide-inline" /> {i18nT('pages.chat.chatFooter.compacting')}</span>
        ) : (
          // Both branches render THEME-SUPPLIED components (a whole replacement
          // loader, or the icons the carousel cycles). A throwing one must not
          // escape: unguarded it reaches the route-level ErrorBoundary and swaps
          // the entire chat UI for the error card. The loader is decorative, so
          // failing it closed to nothing is strictly better than losing the chat.
          // `fallback={null}` is honoured explicitly (ErrorBoundary tests
          // `'fallback' in props`), so this renders nothing rather than a card.
          <ErrorBoundary fallback={null}>
            {loader.kind === 'custom'
              // The theme replaced the whole loader — it owns its size and motion.
              ? <loader.Component />
              : <SwapCarousel icons={loader.icons} />}
          </ErrorBoundary>
        )}
      </div>
    </div>
  )
})

export default ChatFooter
