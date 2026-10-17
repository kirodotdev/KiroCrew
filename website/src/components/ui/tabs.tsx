import * as React from 'react'
import * as TabsPrimitive from '@radix-ui/react-tabs'
import { motion, useReducedMotion } from 'framer-motion'

import { cn } from '../../lib/utils'
import { autoSiteRef } from '../../uiLocations/targetRegistry'
import { MaybeGuideRevealScope } from '../../guide/GuideRevealScope'
import type { GuideRevealScopeId } from '../../uiLocations/guidePlans.gen'
import { useProbeTarget } from '../../guide/probeRegistry'
import {
  TABS_COUNT_BASE_CLASS,
  TABS_INDICATOR_CLASS,
  TABS_INDICATOR_SPRING,
  TABS_SEGMENT_CLASS,
  TABS_SEGMENT_DISABLED_ARIA_CLASS,
  TABS_TRACK_CLASS,
} from './tabsPill'

/**
 * shadcn-style wrapper around Radix Tabs, themed to match `SegmentedControl` —
 * the pill this codebase already uses everywhere for switching between views.
 * The two are the same control to look at; the difference is what they MEAN.
 *
 *   Tabs (here)       — NAVIGATION between panels: which screen am I on. Carries
 *                       the WAI-ARIA tabs contract, so the panel below is wired
 *                       to the selected tab and announced as such.
 *   SegmentedControl  — a FILTER over one view: which subset am I looking at.
 *                       A row of toggles, no panel relationship.
 *
 * Radix supplies exactly the part a hand-rolled pill row cannot: roving tabindex
 * (the whole rail is ONE tab stop), arrow keys / Home / End, `aria-selected`, and
 * the `aria-controls` ⇄ `aria-labelledby` pair between a trigger and its panel.
 *
 * `TabsTrigger` emits `aria-controls` UNCONDITIONALLY, so a `TabsList` without a
 * matching `TabsContent` leaves that attribute pointing at an element that does
 * not exist. A surface whose body is one shared subtree parameterised by the
 * active tab — rather than one panel per tab — therefore does not belong on this
 * component; it needs a tablist with no panel linkage at all.
 *
 * The selected pill SLIDES, sharing one framer-motion `layoutId` across the
 * triggers exactly as `SegmentedControl` does — which is why the indicator is an
 * absolutely-positioned element the active trigger mounts, rather than a
 * `data-[state=active]:` background: a background cannot animate between two
 * different DOM nodes. This is the one place in `ui/` that uses framer-motion,
 * and it is deliberate: the alternative is that these five rails swap instantly
 * while the sixteen filter rails beside them slide, which reads as two different
 * controls. Consequence worth knowing — two rails on ONE page must be given
 * different `layoutId`s, or their indicators animate into each other.
 *
 * Deliberate deviations from shadcn stock:
 *
 *   - Density follows `SegmentedControl`, not upstream: 12px type and a
 *     `p-0.5` track, against shadcn's `h-9` + `text-sm` + `flex-1` equal-width
 *     segments, which sit noticeably looser than every other control here.
 *   - The active segment is marked with the ACCENT colour, matching the rest of
 *     the app's selected states, where shadcn uses plain foreground.
 *   - Focus uses the `.focus-ring` utility rather than the global
 *     `:focus-visible` outline: that outline is `outline-offset: 2px`, which on a
 *     pill inside a 2px track paints a box straddling the track's own border.
 */

/**
 * Which tab is selected, and the `layoutId` its indicator rides on. Radix keeps
 * its own value in a private context, so the sliding indicator needs this one to
 * know which trigger should be mounting it.
 */
interface TabsValue {
  current?: string
  layoutId: string
  /** `guideProbe="local"` rails only: what a guide's probe drives. */
  probe?: { currentRef: React.MutableRefObject<string | undefined>; select: (value: string) => void }
}

const TabsValueContext = React.createContext<TabsValue | null>(null)

interface TabsProps extends React.ComponentPropsWithoutRef<typeof TabsPrimitive.Root> {
  /** Distinguishes this rail's sliding indicator from another rail's on the same page. */
  layoutId?: string
  /**
   * `local`: the selected tab lives in component state only, never in the
   * address or history, so a guide's `ui.find` probe may select another tab
   * to look inside and select this one back (`guide/probeRegistry.ts`).
   * Left unset, the probe never touches the rail: a tab that navigates is
   * the person's to press.
   */
  guideProbe?: 'local'
}

const Tabs = React.forwardRef<React.ComponentRef<typeof TabsPrimitive.Root>, TabsProps>(
  function Tabs({ layoutId = 'tabs-indicator', value, defaultValue, onValueChange, guideProbe, ...props }, ref) {
    // Mirrors the value for the UNCONTROLLED case too, so a caller using
    // `defaultValue` still gets the sliding indicator rather than silently
    // falling back to no marker at all.
    const [internal, setInternal] = React.useState(value ?? defaultValue)
    const current = value ?? internal
    const handleValueChange = React.useCallback((next: string) => {
      setInternal(next)
      onValueChange?.(next)
    }, [onValueChange])
    const currentRef = React.useRef(current)
    currentRef.current = current
    const local = guideProbe === 'local'
    const probe = React.useMemo(() => (local ? { currentRef, select: handleValueChange } : undefined), [local, handleValueChange])
    const ctx = React.useMemo(() => ({ current, layoutId, probe }), [current, layoutId, probe])

    // Radix is always handed the value, so a probe's selection (state, not a
    // press) is what the rail shows.
    return (
      <TabsValueContext.Provider value={ctx}>
        <TabsPrimitive.Root
          ref={ref}
          value={current}
          onValueChange={handleValueChange}
          {...props}
        />
      </TabsValueContext.Provider>
    )
  },
)
Tabs.displayName = TabsPrimitive.Root.displayName

const TabsList = React.forwardRef<
  React.ComponentRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(function TabsList({ className, ...props }, ref) {
  return (
    <TabsPrimitive.List ref={ref} className={cn(TABS_TRACK_CLASS, className)} {...props} />
  )
})
TabsList.displayName = TabsPrimitive.List.displayName

const TabsTrigger = React.forwardRef<
  React.ComponentRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(function TabsTrigger({ className, children, value, ...props }, ref) {
  const ctx = React.useContext(TabsValueContext)
  const reduceMotion = useReducedMotion()
  const isActive = ctx?.current === value
  const el = React.useRef<HTMLButtonElement | null>(null)
  const setRef = React.useCallback((node: HTMLButtonElement | null) => {
    el.current = node
    if (typeof ref === 'function') ref(node)
    else if (ref) ref.current = node
  }, [ref])
  const uiAuto = (props as Record<string, unknown>)['data-ui-auto']
  const registeredRef = autoSiteRef<HTMLButtonElement>(uiAuto, setRef)
  const probe = ctx?.probe
  useProbeTarget(!!probe && !props.disabled, {
    kind: 'tab',
    trigger: () => el.current,
    isOpen: () => probe?.currentRef.current === value,
    open: () => {
      const prior = probe?.currentRef.current
      probe?.select(value)
      return () => { if (prior !== undefined && probe?.currentRef.current === value) probe.select(prior) }
    },
  })

  return (
    <TabsPrimitive.Trigger
      ref={registeredRef}
      value={value}
      className={cn(
        TABS_SEGMENT_CLASS,
        // Radix marks the selected trigger itself, so the accent rides a
        // `data-state` variant here rather than a prop the caller passes.
        'data-[state=active]:text-accent',
        // A disabled tab keeps its tooltip reachable, so it must stay focusable —
        // hence `aria-disabled` from the caller rather than the `disabled` prop,
        // and the greying is styled off that instead of `:disabled`.
        TABS_SEGMENT_DISABLED_ARIA_CLASS,
        className,
      )}
      {...props}
    >
      {/* Reduced motion also excludes shared-layout projection, not only its duration. */}
      {isActive && (reduceMotion
        ? <span aria-hidden="true" className={TABS_INDICATOR_CLASS} />
        : <motion.span
            layoutId={ctx?.layoutId}
            aria-hidden="true"
            className={TABS_INDICATOR_CLASS}
            transition={TABS_INDICATOR_SPRING}
          />
      )}
      {children}
    </TabsPrimitive.Trigger>
  )
})
TabsTrigger.displayName = TabsPrimitive.Trigger.displayName

const TabsContent = React.forwardRef<
  React.ComponentRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content> & {
    /**
     * The compiled reveal scope this panel is (`tab:<trigger location id>`):
     * it reports selected/not to a running guide. The owner wraps the Radix
     * panel, so it reports "closed" while an inactive panel is unmounted.
     */
    guideScope?: GuideRevealScopeId
  }
>(function TabsContent({ className, guideScope, ...props }, ref) {
  const ctx = React.useContext(TabsValueContext)
  return (
    <MaybeGuideRevealScope id={guideScope} open={ctx?.current === props.value}>
      <TabsPrimitive.Content
        ref={ref}
        // Radix puts `tabindex=0` on the panel so the rail's one tab stop leads
        // into it; that makes the panel itself focusable, and the global outline
        // would then ring the entire page body.
        className={cn('focus-visible:outline-hidden', className)}
        {...props}
      />
    </MaybeGuideRevealScope>
  )
})
TabsContent.displayName = TabsPrimitive.Content.displayName

/**
 * Trailing count for a tab that owns a collection. Zero and absent both render
 * nothing — a "0" badge is noise, not information — so a caller can pass the
 * length unconditionally.
 *
 * It renders INSIDE the trigger, so the count joins the tab's accessible name.
 * A tab with no count is therefore named by its label alone, which is what an
 * `exact: true` name query in the browser suite relies on.
 */
function TabsCount({ value }: { value?: number }) {
  if ((value ?? 0) <= 0) return null
  return (
    <span className={cn(TABS_COUNT_BASE_CLASS, 'text-muted/40 group-data-[state=active]/tab:text-accent/60')}>
      {value}
    </span>
  )
}

/**
 * The `{key, label, icon, count}` shape call sites already build their rails
 * from. Declared here so a page keeps describing its tabs as data while the
 * triggers themselves are composed — which is what lets a caller put the count,
 * an icon, or nothing at all inside a trigger without this file knowing.
 */
export interface TabItem<T extends string = string> {
  key: T
  label: string
  icon?: React.ReactNode
  count?: number
}

export { Tabs, TabsList, TabsTrigger, TabsContent, TabsCount }
