import React from 'react'
import { twMerge } from 'tailwind-merge'
import { motion, useMotionValue, useSpring, useMotionTemplate, useReducedMotion } from 'framer-motion'
import InfoTip from './InfoTip'
import { i18nT } from '../i18n/t'

/* ── Shared UI primitives ── */

export function Card({ children, className = '', ...rest }: Omit<React.ComponentPropsWithoutRef<'div'>, 'dangerouslySetInnerHTML'>) {
  return (
    <div className={twMerge('card-glow border border-border bg-card rounded-lg p-5 mb-4 animate-rise shadow-sm transition-all', className)} {...rest}>
      {children}
    </div>
  )
}

export function CardTitle({ children, className, ...rest }: Omit<React.ComponentPropsWithoutRef<'h3'>, 'dangerouslySetInnerHTML'>) {
  return <h3 className={twMerge("text-sm font-semibold tracking-tight text-text-strong mb-3.5 flex items-center gap-2", className)} {...rest}>{children}</h3>
}

export const Btn = React.forwardRef<HTMLButtonElement, React.ButtonHTMLAttributes<HTMLButtonElement> & { danger?: boolean; primary?: boolean }>(
  ({ children, danger, primary, className, ...rest }, ref) => (
    <button
      ref={ref}
      className={twMerge(`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[13px] cursor-pointer font-body transition-all active:scale-[0.97] active:duration-75 disabled:opacity-30 disabled:cursor-not-allowed ${
        primary
          ? 'bg-accent text-accent-fg border-accent hover:bg-accent-hover hover:shadow-[0_0_12px_var(--accent-glow)]'
          : danger
            ? 'border-border bg-transparent text-muted hover:text-danger hover:border-danger'
            : 'border-border bg-transparent text-muted hover:text-text hover:border-border-strong hover:bg-bg-hover'
      }`, className)}
      {...rest}
    >
      {children}
    </button>
  )
)

export function SendBtn({ children, onClick, disabled, style, className, ...rest }: { children: React.ReactNode } & Omit<React.ComponentPropsWithoutRef<'button'>, 'children' | 'dangerouslySetInnerHTML'>) {
  return (
    <button
      className={twMerge("btn-sweep bg-accent text-accent-fg border-none rounded-lg px-4 h-9 text-sm font-semibold cursor-pointer hover:bg-accent-hover hover:shadow-[0_0_20px_var(--accent-glow)] disabled:opacity-30 disabled:cursor-not-allowed transition-all font-body", className)}
      onClick={onClick}
      disabled={disabled}
      style={style}
      {...rest}
    >
      {children}
    </button>
  )
}

/* ── Icon button group ──
 * The hover-revealed capsule of small square icon buttons used in chat session
 * rows, the widget header, and other row/card affordances. `IconButtonGroup`
 * is the bordered `bg-card` capsule; pass `reveal` to fade it in on the parent's
 * :hover / focus-within (the parent element must carry the `group` class).
 * `IconButton` is a single square icon button; `variant` sets the hover color. */
const ICON_BUTTON_VARIANTS: Record<'default' | 'accent' | 'danger' | 'active', string> = {
  default: 'text-muted hover:text-text hover:bg-bg-hover',
  accent: 'text-muted hover:text-accent hover:bg-bg-hover',
  danger: 'text-muted hover:text-danger hover:bg-danger-subtle',
  active: 'text-accent hover:bg-bg-hover',
}

export const IconButton = React.forwardRef<
  HTMLButtonElement,
  Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'> & { variant?: keyof typeof ICON_BUTTON_VARIANTS; 'aria-label': string }
>(({ variant = 'default', className, children, ...rest }, ref) => (
  <button
    ref={ref}
    type="button"
    className={twMerge(
      'p-[4px] rounded transition-all active:scale-[0.97] active:duration-75 cursor-pointer bg-transparent border-none disabled:opacity-30 disabled:cursor-not-allowed',
      ICON_BUTTON_VARIANTS[variant],
      className,
    )}
    {...rest}
  >
    {children}
  </button>
))

export function IconButtonGroup({ reveal, className, children }: { reveal?: boolean; className?: string; children: React.ReactNode }) {
  return (
    <div
      className={twMerge(
        'flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm transition-all',
        reveal ? 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100' : '',
        className,
      )}
    >
      {children}
    </div>
  )
}

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className = '', ...props }, ref) => (
    <input
      ref={ref}
      className={twMerge('bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none flex-1 transition-colors focus-ring', className)}
      {...props}
    />
  )
)

export function SearchInput({ className = '', ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className={`relative ${className}`}>
      <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted pointer-events-none stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input
        className="w-full bg-bg-elevated border border-border rounded-md pl-7 pr-3 py-1.5 text-text text-[13px] font-body outline-none transition-all focus-ring placeholder:text-muted/50"
        {...props}
      />
    </div>
  )
}

export function Badge({ variant, children, className, ...rest }: { variant: 'ok' | 'err' | 'warn' | 'aim' | 'muted' } & Omit<React.ComponentPropsWithoutRef<'span'>, 'children' | 'dangerouslySetInnerHTML'> & { children: React.ReactNode }) {
  const cls =
    variant === 'ok' ? 'bg-ok-subtle text-ok'
    : variant === 'err' ? 'bg-danger-subtle text-danger'
    : variant === 'aim' ? 'bg-aim-subtle text-aim'
    : variant === 'muted' ? 'bg-[var(--bg-hover)] text-[var(--muted)]'
    : 'bg-warn-subtle text-warn'
  return (
    <span className={twMerge(`inline-flex items-center gap-1 px-2 py-[2px] rounded-full text-[13px] font-medium font-mono whitespace-nowrap hover:scale-105 transition-transform ${cls}`, className)} {...rest}>
      {children}
    </span>
  )
}

export function SourceBadge({ source }: { source: string }) {
  const cls =
    source === 'package' ? 'bg-aim-subtle text-aim border-aim/30'
    : source === 'kirocrew' ? 'bg-bg-elevated text-muted border-border'
    : source === 'project' ? 'text-ok border-ok/30'
    : 'bg-bg-elevated text-muted border-border'
  return <span className={`px-1.5 py-[2px] rounded-full text-[11px] font-bold border shrink-0 ${cls}`}>{source}</span>
}

export function StatCard({ label, value, accent, colorClass, delay, onClick, active, title, className, ...rest }: { label: string; value?: string | number | null; accent?: boolean; colorClass?: string; delay?: number; onClick?: () => void; active?: boolean; title?: string } & Omit<React.ComponentPropsWithoutRef<'div'>, 'title' | 'onClick' | 'dangerouslySetInnerHTML'>) {
  const loading = value === undefined || value === null
  return (
    // role/tabIndex/onKeyDown are all applied together with onClick, so the card
    // is fully keyboard-accessible whenever it is interactive; eslint can't see
    // the conditional role, hence the scoped disables.
    /* eslint-disable-next-line jsx-a11y/no-static-element-interactions */
    <div
      className={twMerge(`stat-accent relative overflow-hidden bg-card rounded-md px-4 py-3.5 border shadow-[inset_0_1px_0_var(--card-hl)] animate-rise hover:border-border-strong hover:-translate-y-0.5 hover:shadow-md transition-all ${active ? 'border-accent ring-1 ring-accent/40' : 'border-border'} ${onClick ? 'cursor-pointer' : ''}`, className)}
      style={delay ? { animationDelay: `${delay}ms` } : undefined}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } } : undefined}
      // Placed before {...rest} so a consumer can override it with a more
      // specific id (e.g. data-testid="stat-card-unread") when one card among
      // several needs to be addressed directly.
      data-testid="stat-card"
      {...rest}
    >
      <div className="text-muted text-[13px] font-medium uppercase tracking-[.04em] flex items-center gap-1" data-testid="stat-card-label">{label}{title && <InfoTip text={title} />}</div>
      {loading
        ? <div className="skeleton h-7 w-16 mt-1.5 rounded" data-testid="stat-card-skeleton" />
        : <div className={`text-2xl font-bold mt-1.5 tracking-tight leading-none ${accent ? 'text-accent' : colorClass || ''}`} data-testid="stat-card-value">{value ?? '—'}</div>
      }
    </div>
  )
}

/** Shared loading-placeholder box. Mirrors shadcn's Skeleton: a pulsing rounded
 *  box you size via `className`. (shadcn uses `bg-accent`; we map that neutral to
 *  this app's `--bg-hover` token.) */
export function Skeleton({ className, ...props }: React.ComponentProps<'div'>) {
  return <div data-slot="skeleton" className={twMerge('bg-bg-hover animate-pulse rounded-md', className)} {...props} />
}

export function ContentSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-4">
      <Skeleton className="h-5 w-48" />
      <Skeleton className="h-3 w-72" />
      <div className="space-y-2 mt-4">
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="flex items-center gap-3">
            <Skeleton className="h-4 w-4" />
            <Skeleton className="h-4 flex-1" style={{ maxWidth: `${60 + (i * 17) % 30}%` }} />
          </div>
        ))}
      </div>
    </div>
  )
}

/* ── Form-shaped loading skeleton ──
 * Composes the shared <Skeleton> box into form-row shapes (label box +
 * full-width control box / toggle pill / info value). Reusable across any
 * form-like view: compose the rows directly or pass a `rows` spec to FormSkeleton.
 */

/** Label block on the left, toggle pill on the right. */
export function SkeletonToggleRow() {
  return (
    <div className="flex items-center justify-between py-1.5">
      <Skeleton className="h-9 w-44 max-w-[55%]" />
      <Skeleton className="h-5 w-9 rounded-full shrink-0" />
    </div>
  )
}

/** Small label box above a full-width control box (select / input). */
export function SkeletonField() {
  return (
    <div className="flex flex-col gap-2 py-1.5">
      <Skeleton className="h-4 w-28" />
      <Skeleton className="h-9 w-full" />
    </div>
  )
}

/** Read-only info row: label box on the left, short value box on the right. */
export function SkeletonInfoRow() {
  return (
    <div className="flex items-center justify-between py-1.5">
      <Skeleton className="h-4 w-24" />
      <Skeleton className="h-5 w-16" />
    </div>
  )
}

export type SkeletonRowKind = 'toggle' | 'field' | 'info'

/** Compose a form skeleton from a row spec, e.g. ['toggle','field','field','info']. */
export function FormSkeleton({ rows }: { rows: SkeletonRowKind[] }) {
  return (
    <>
      {rows.map((kind, i) =>
        kind === 'toggle' ? <SkeletonToggleRow key={i} />
          : kind === 'info' ? <SkeletonInfoRow key={i} />
            : <SkeletonField key={i} />,
      )}
    </>
  )
}

/**
 * @param testId Base `data-testid` for this instance. Several EmptyStates can be
 *   mounted on one page (AppsPage renders 3; /notifications renders the page's own
 *   plus NotificationFeed's), so a single shared id makes a Playwright lookup
 *   ambiguous. Consumers on such pages pass a specific base; the title and
 *   subtitle derive `<base>-title` / `<base>-subtitle` from it.
 * @param action Optional CTA rendered below the subtitle. Accepts a ReactNode
 *   (typically a `<SendBtn>` or `<Btn>`) so the empty state guides the user
 *   toward populating the surface.
 */
export function EmptyState({ icon, title, subtitle, action, testId = 'empty-state' }: { icon: React.ReactNode; title: string; subtitle?: string; action?: React.ReactNode; testId?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 gap-2 animate-rise" data-testid={testId}>
      <div className="text-[40px] opacity-[.12] select-none">{icon}</div>
      <div className="text-muted text-sm font-medium" data-testid={`${testId}-title`}>{title}</div>
      {subtitle && <div className="text-muted/60 text-[13px] text-center max-w-[320px]" data-testid={`${testId}-subtitle`}>{subtitle}</div>}
      {action && <div className="mt-2" data-testid={`${testId}-action`}>{action}</div>}
    </div>
  )
}

/**
 * Shown when a filter/search reduces a non-empty list to zero results.
 * Echoes the query back so the user sees what excluded everything, and offers
 * a clear-filter action. Visually lighter than EmptyState (no large icon) to
 * signal "your data exists, your filter hid it" rather than "nothing here yet".
 */
export function FilteredEmpty({ query, onClear, noun, testId = 'filtered-empty' }: { query: string; onClear: () => void; noun?: string; testId?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-8 gap-1.5 animate-rise" data-testid={testId}>
      <div className="text-muted text-sm">
        {noun
          ? i18nT('components.ui.no_noun_match', { noun })
          : i18nT('components.ui.no_matches_for')}{' '}
        <span className="font-medium text-text">&ldquo;{query}&rdquo;</span>
      </div>
      <button
        onClick={onClear}
        className="text-accent text-[13px] hover:underline cursor-pointer bg-transparent border-none p-0"
        data-testid={`${testId}-clear`}
      >
        {i18nT('components.ui.clear_filter')}
      </button>
    </div>
  )
}

/**
 * Header for one counted list group inside a side panel: a sentence-case label,
 * the group's count as its own token, and a hairline rule filling the remaining
 * width so one group reads as separated from the next.
 *
 * Shared on purpose. The chat side panel's Files and Artifacts tabs each
 * hand-rolled a header, and the two diverged on every axis — case, size,
 * weight, colour, whether the count was a node or punctuation baked into the
 * label, and whether a rule was drawn at all — so one panel looked like two.
 * Route new panel sections through here instead of adding a third idiom.
 *
 * Two properties are deliberate rather than incidental:
 *
 *  - Hierarchy comes from WEIGHT and SIZE, never from opacity. The idiom this
 *    replaced dimmed the label to `text-muted/40` (~1.7:1 against the panel on
 *    both default themes) and the count to `text-muted/50` (~1.9:1), well under
 *    WCAG 1.4.3's 4.5:1 for text this small. Full `text-muted` clears it.
 *  - The label is NOT uppercased. `text-transform` is a no-op on CJK, so an
 *    uppercase micro-header keeps its hierarchy in English and loses it
 *    entirely in zh-CN / ja / ko.
 *
 * @param count Rendered as a separate node, so no translated label has to carry
 *   `(%{count})` punctuation — that interpolation was a concatenation seam.
 * @param trailing Placed AFTER the rule (e.g. a "resolving links…" pulse), so a
 *   transient indicator cannot push the label or count around.
 */
export function PanelSectionHeader({ label, count, trailing, className }: {
  label: string
  count?: number
  trailing?: React.ReactNode
  className?: string
}) {
  return (
    <div className={twMerge('flex items-center gap-2', className)} data-testid="panel-section-header">
      <span className="text-[11.5px] font-semibold text-muted">{label}</span>
      {count !== undefined && (
        <span className="text-[10.5px] text-muted font-mono tabular-nums">{count}</span>
      )}
      <span className="flex-1 h-px bg-border" />
      {trailing}
    </div>
  )
}

export function PageHeader({ title, subtitle, actions }: { title: React.ReactNode; subtitle?: string; actions?: React.ReactNode }) {
  return (
    <div className="flex items-end justify-between gap-4 px-6 pt-2 pb-3" data-testid="page-header">
      <div>
        <div className="text-2xl font-bold tracking-tight text-text-strong" data-testid="page-title">{title}</div>
        {subtitle && <div className="text-muted text-sm mt-1" data-testid="page-subtitle">{subtitle}</div>}
      </div>
      {actions && <div className="flex items-center gap-2.5 flex-wrap justify-end">{actions}</div>}
    </div>
  )
}

export function Toggle({ checked, onChange, disabled, label, tone = 'accent' }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string; tone?: 'accent' | 'muted' }) {
  return (
    <div
      role="switch"
      aria-checked={checked}
      // Without this a screen reader announces a disabled switch as actionable:
      // the disabled state was carried only by `tabIndex={-1}` and an opacity
      // class, neither of which reaches the accessibility tree.
      aria-disabled={disabled || undefined}
      aria-label={label}
      tabIndex={disabled ? -1 : 0}
      onClick={() => !disabled && onChange(!checked)}
      onKeyDown={e => { if (!disabled && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); onChange(!checked) } }}
      // `muted` is for a LIST of switches, where an accent fill on every row
      // shouts and duplicates a state the row's own grouping already carries.
      // The knob position still reads the state, so nothing is lost by dropping
      // the hue. Accent stays the default so a lone switch keeps its emphasis.
      className={`w-9 h-5 rounded-full relative transition-colors shrink-0 cursor-pointer ${disabled ? 'opacity-40 cursor-not-allowed' : ''} ${checked ? (tone === 'muted' ? 'bg-border-strong' : 'bg-accent') : 'bg-border'}`}
    >
      <div className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-[18px]' : 'translate-x-0.5'}`} />
    </div>
  )
}

/* ── Slider ── */

export interface SliderProps {
  value: number
  onChange: (value: number) => void
  min?: number
  max?: number
  /** Snap increment. Discrete tick marks render automatically for small, even step counts. */
  step?: number
  disabled?: boolean
  /** Label shown above the track (left-aligned). */
  label?: string
  /** Show the numeric value above the track (right-aligned). */
  showValue?: boolean
  /** Custom formatter for the displayed / announced value (e.g. v => `${v}%`). */
  formatValue?: (v: number) => string
  /** Force tick marks on/off. Defaults to auto (on when 2–40 even steps). */
  ticks?: boolean
  /** When true, the knob pulses an accent halo while parked at the max notch. */
  emphasizeMax?: boolean
  className?: string
  'aria-label'?: string
}

/** macOS-style range slider: accent fill, circular knob, optional step ticks.
 *  Fully custom (not <input type="range">) for precise theming. Controlled —
 *  pass `value` + `onChange`. Supports drag, click-to-seek, and full keyboard
 *  (arrows = step, Shift+arrow / PageUp-Down = ×10, Home/End = min/max). */
export function Slider({
  value, onChange, min = 0, max = 100, step = 1, disabled,
  label, showValue, formatValue, ticks, emphasizeMax, className = '', 'aria-label': ariaLabel,
}: SliderProps) {
  const trackRef = React.useRef<HTMLDivElement>(null)
  const [dragging, setDragging] = React.useState(false)
  const pointerDown = React.useRef(false)
  const [hoverVal, setHoverVal] = React.useState<number | null>(null)
  const reduceMotion = useReducedMotion()

  const range = (max - min) || 1
  const clamp = (v: number) => Math.min(max, Math.max(min, v))
  const snap = (v: number) => {
    if (step <= 0) return clamp(v)
    const snapped = Math.round((v - min) / step) * step + min
    const decimals = (String(step).split('.')[1] || '').length
    return clamp(Number(snapped.toFixed(decimals)))
  }
  const current = clamp(value)
  const pct = ((current - min) / range) * 100
  const display = formatValue ? formatValue(current) : String(current)
  const atMax = emphasizeMax && current >= max

  // Discrete-stepper detection: a small, even number of steps. Discrete sliders
  // render tick marks AND spring to each notch even while dragging; continuous
  // sliders (many/fine steps) track the pointer 1:1 with no spring lag.
  const segments = step > 0 ? range / step : 0
  const showTicks = ticks ?? (step > 0 && Number.isInteger(segments) && segments >= 2 && segments <= 40)
  const tickCount = Math.round(segments)
  const tickFracs = showTicks ? Array.from({ length: tickCount + 1 }, (_, i) => i / tickCount) : []

  // KNOB px width. Knob center, fill end, and every tick all use the SAME
  // travel formula so they line up exactly: the center sweeps [r, W-r] as the
  // value goes min→max, keeping the knob fully inside the track at both ends.
  const KNOB = 18
  const center = (f: number) => `calc(${f} * (100% - ${KNOB}px) + ${KNOB / 2}px)`

  // Spring the knob/fill along the track. Discrete steppers spring to each
  // notch even while dragging (snappy stepped feel); continuous sliders jump
  // to the pointer so there's zero lag under the finger.
  const targetFrac = useMotionValue(pct / 100)
  const springFrac = useSpring(targetFrac, { stiffness: 300, damping: 26, mass: 0.9 })
  React.useEffect(() => {
    targetFrac.set(pct / 100)
    if (dragging && !showTicks) springFrac.jump(pct / 100)
  }, [pct, dragging, showTicks, targetFrac, springFrac])
  const motionPos = useMotionTemplate`calc(${springFrac} * (100% - ${KNOB}px) + ${KNOB / 2}px)`

  const setFromClientX = (clientX: number) => {
    const el = trackRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const usable = rect.width - KNOB
    const frac = usable > 0 ? (clientX - rect.left - KNOB / 2) / usable : 0
    const next = snap(min + Math.min(1, Math.max(0, frac)) * range)
    if (next !== value) onChange(next)
  }

  const onPointerDown = (e: React.PointerEvent) => {
    if (disabled) return
    e.preventDefault()
    // preventDefault suppresses the implicit focus from the pointer gesture, so
    // focus the track explicitly — otherwise arrow keys after a click-to-seek go
    // to whatever had focus before instead of the slider.
    trackRef.current?.focus()
    ;(e.target as Element).setPointerCapture?.(e.pointerId)
    pointerDown.current = true
    // A click (down without move) is a discrete seek — leave `dragging` false
    // so the knob springs to the new spot. Movement below flips into drag mode.
    setFromClientX(e.clientX)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    if (disabled) return
    // Track the step under the cursor for the hover tooltip (hover or drag).
    const el = trackRef.current
    if (el) {
      const rect = el.getBoundingClientRect()
      const usable = rect.width - KNOB
      const frac = usable > 0 ? Math.min(1, Math.max(0, (e.clientX - rect.left - KNOB / 2) / usable)) : 0
      setHoverVal(snap(min + frac * range))
    }
    if (!pointerDown.current) return
    if (!dragging) setDragging(true) // first move → drag mode (lag-free jump)
    setFromClientX(e.clientX)
  }
  const onPointerLeave = () => { if (!pointerDown.current) setHoverVal(null) }
  const endDrag = (e: React.PointerEvent) => {
    if (!pointerDown.current) return
    pointerDown.current = false
    ;(e.target as Element).releasePointerCapture?.(e.pointerId)
    setDragging(false)
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return
    const big = step * 10
    let next = value
    switch (e.key) {
      case 'ArrowRight': case 'ArrowUp':   next = value + (e.shiftKey ? big : step); break
      case 'ArrowLeft':  case 'ArrowDown': next = value - (e.shiftKey ? big : step); break
      case 'PageUp':   next = value + big; break
      case 'PageDown': next = value - big; break
      case 'Home': next = min; break
      case 'End':  next = max; break
      default: return
    }
    e.preventDefault()
    next = snap(next)
    if (next !== value) onChange(next)
  }


  return (
    <div className={`relative flex flex-col gap-1.5 ${disabled ? 'opacity-40' : ''} ${className}`}>
      {(label || showValue) && (
        <div className="flex items-center justify-between">
          {label ? <span className="text-[13px] font-semibold text-text">{label}</span> : <span />}
          {showValue && <span className="text-[12px] text-muted tabular-nums">{display}</span>}
        </div>
      )}

      {/* The track is the single interactive control: role=slider, owns pointer
          (drag + click-to-seek) and keyboard. The knob below is decorative. */}
      <div
        ref={trackRef}
        role="slider"
        aria-label={ariaLabel || label}
        aria-valuemin={min}
        aria-valuemax={max}
        aria-valuenow={current}
        aria-valuetext={display}
        aria-orientation="horizontal"
        aria-disabled={disabled || undefined}
        tabIndex={disabled ? -1 : 0}
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onPointerLeave={onPointerLeave}
        className={`group relative h-[18px] flex items-center select-none touch-none outline-none ${disabled ? 'cursor-not-allowed' : 'cursor-pointer'}`}
      >
        {/* groove */}
        <div className="absolute left-0 right-0 top-1/2 -translate-y-1/2 h-1 rounded-full bg-border" />
        {/* filled — springs with the knob, ending at its center */}
        <motion.div className="absolute left-0 top-1/2 -translate-y-1/2 h-1 rounded-full bg-accent overflow-hidden" style={{ width: motionPos }}>
          {atMax && (
            <motion.div
              aria-hidden
              className="absolute inset-y-0 w-1/3"
              style={{ background: 'linear-gradient(90deg, transparent, rgba(255,255,255,.85), transparent)' }}
              animate={{ x: ['-120%', '420%'] }}
              transition={{ repeat: Infinity, duration: 1.6, ease: 'easeInOut', repeatDelay: 0.3 }}
            />
          )}
        </motion.div>
        {/* notches on the bar itself */}
        {showTicks && tickFracs.map((f, i) => (
          <span
            key={`bar-${i}`}
            aria-hidden
            className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2 w-[2px] h-1 rounded-full bg-muted opacity-40"
            style={{ left: center(f) }}
          />
        ))}
        {/* hover/drag tooltip — value of the step under the cursor */}
        {hoverVal !== null && (
          <div
            aria-hidden
            className="pointer-events-none absolute -translate-x-1/2 z-30"
            style={{ left: center((hoverVal - min) / range), bottom: 'calc(100% + 6px)' }}
          >
            <span className="whitespace-nowrap rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] font-medium text-text shadow-md">
              {formatValue ? formatValue(hoverVal) : String(hoverVal)}
            </span>
          </div>
        )}
        {/* knob (decorative — the track is the control). Positioner springs to
            the value; the inner circle owns press/drag scale + focus ring. */}
        <motion.div aria-hidden className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2" style={{ left: motionPos }}>
          <motion.div
            className="relative w-[18px] h-[18px] rounded-full bg-white border border-black/10 shadow-[0_1px_3px_rgba(0,0,0,.3),0_0.5px_1px_rgba(0,0,0,.2)] group-focus-visible:ring-2 group-focus-visible:ring-[var(--ring)] group-focus-visible:ring-offset-1 group-focus-visible:ring-offset-[var(--bg)]"
            animate={{ scale: reduceMotion ? 1 : (dragging ? 1.15 : 1), boxShadow: dragging ? '0 2px 7px rgba(0,0,0,.4)' : atMax ? '0 0 10px var(--accent)' : '0 1px 3px rgba(0,0,0,.3)' }}
            whileHover={disabled || reduceMotion ? undefined : { scale: 1.12 }}
            whileTap={disabled || reduceMotion ? undefined : { scale: 1.2 }}
            transition={reduceMotion ? { duration: 0 } : { type: 'spring', stiffness: 500, damping: 26 }}
          />
        </motion.div>
      </div>

      {/* discrete tick marks under the groove (static reference; the hover
          tooltip lives on the bar itself) */}
      {showTicks && (
        <div className="relative h-[5px]" aria-hidden>
          {tickFracs.map((f, i) => (
            <span
              key={i}
              className="absolute top-0 -translate-x-1/2 w-[1.5px] h-[5px] rounded-full bg-muted opacity-40"
              style={{ left: center(f) }}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/** Bare themed checkbox. Pairs with a caller-supplied label. */
export const Checkbox = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ style, ...rest }, ref) => (
    <input
      ref={ref}
      type="checkbox"
      style={{ margin: 0, accentColor: 'var(--accent)', cursor: 'pointer', ...style }}
      {...rest}
    />
  ),
)
Checkbox.displayName = 'Checkbox'

/* There is deliberately no `Select` here any more.
 *
 * This module used to export one, and it wrapped a native `<select>`: the closed
 * trigger picked up the theme, but the OPEN popup was drawn by the OS, so it
 * ignored every theme token and could not be styled per row. Dropdowns now go
 * through `ui/select.tsx` (Radix) via `SimpleSelect` / `SettingsSelect`, or
 * `SearchableSelect` when the list is long enough to need a filter box.
 * See website/docs/page-layout.md. */
