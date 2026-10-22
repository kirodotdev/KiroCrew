import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { Loader2 } from 'lucide-react'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import { useAppSelector } from '../store'
import { isEmbeddedPane } from '../lib/embedded'
import { useSelectInstance } from '../hooks/useSelectInstance'
import CrewIdentityMark from './CrewIdentityMark'
import { errMessage } from '../utils/thunkError'
import { i18nT } from '../i18n/t'
import { postToHost } from '../lib/hostRelay'
import { useOverlayRoot } from './overlayRoot'
import { useNavTip } from '../hooks/useNavTip'
import { LOCAL_VALUE, RailListError, stateDotCls, useCrewPins, visibleInstanceTabs } from './InstanceTabBar'

/*
 * The pinned-crew rail: one icon tile per pinned crew, under the crew switcher
 * in the desktop nav rail. Split out of InstanceTabBar.tsx (file-line cap); the
 * pin store, crew ordering and switch relay it reads stay there.
 */

/** One pinned-crew icon tile. Null id = the local dashboard. */
function PinnedCrewTile({
  id,
  name,
  title,
  state,
  connecting,
  active,
  onSelect,
}: {
  id: string | null
  name: string
  title: string
  state?: string
  connecting?: boolean
  active: boolean
  onSelect: () => void
}) {
  const overlayRoot = useOverlayRoot()
  // Hover/focus name flyout, the SAME affordance the collapsed nav rows (Sessions,
  // Crewmates, …) use: a portaled label anchored to the tile, so it escapes the
  // rail's overflow clip. Always enabled — a rail tile is always an icon-only row.
  const { tip, tipOn, rowRef, showTip, hideTip } = useNavTip<HTMLButtonElement>(true)
  return (
    <button
      ref={rowRef}
      type="button"
      onClick={onSelect}
      onMouseEnter={showTip}
      onMouseLeave={hideTip}
      onFocus={showTip}
      onBlur={hideTip}
      aria-current={active ? 'true' : undefined}
      aria-label={title}
      data-testid={`rail-pinned-crew-${id ?? LOCAL_VALUE}`}
      // Bare button: no background/border/ring of its own. The CrewIdentityMark
      // below IS the rounded-square tile (its own tinted bg + border), matching
      // the rail's app icon. There is deliberately NO selected/active treatment:
      // the current crew is already shown at the top of the rail, so highlighting
      // it again here is redundant (and was the source of a theme-dependent white
      // plate). `aria-current` still marks it for assistive tech. Inline
      // `outline:none` kills a theme global's 3px rail-outline; keyboard focus
      // gets a clean accent box-shadow that respects the rounded corners.
      style={{ outline: 'none' }}
      className={
        'relative shrink-0 flex items-center justify-center rounded-md p-0 border-0 bg-transparent ' +
        'cursor-pointer transition-transform active:scale-[0.94] hover:brightness-110 ' +
        'focus-visible:[box-shadow:0_0_0_2px_var(--accent)]'
      }
    >
      {/* The SAME artwork and SHAPE the rail's app icon uses — the per-crew tinted
          ghost in a rounded-square tile, or the Kiro Crew mark for Local (id ===
          null) — sized to match the rail tile. A smaller tile needs a smaller
          corner radius (rounded-md) or the square reads as a circle. No selected
          treatment: the current crew is shown at the top of the rail already. A
          connecting crew shows the spinner instead. */}
      {connecting ? (
        <span className="w-[26px] h-[26px] flex items-center justify-center">
          <Loader2 className="lucide-inline animate-spin text-muted" aria-hidden />
        </span>
      ) : (
        <CrewIdentityMark id={id} size={26} radius="rounded-md" surface="var(--bg-accent)" />
      )}
      {/* Live-state dot in the corner, same colour language as the switcher's
          rows. Hidden for a steady `connected` crew so a healthy rail is calm;
          only a non-ok state earns the dot. */}
      {id !== null && !connecting && state && state !== 'connected' ? (
        <span
          className={`absolute -bottom-0.5 -right-0.5 w-2 h-2 rounded-full ring-2 ring-bg-accent ${stateDotCls(state)}`}
          aria-hidden
        />
      ) : null}
      {/* The hover/focus name flyout — portaled to the overlay root, anchored to the
          tile, truncated so a long crew name cannot stretch off-screen. Mirrors
          the collapsed nav-row tooltip (bg-card pill, same fade). */}
      {tip && createPortal(
        <div
          className={`fixed flex items-center pl-3 pr-3 rounded-md bg-card border border-border shadow-lg text-text text-sm font-medium z-[9999] pointer-events-none whitespace-nowrap transition-opacity duration-150 ${tipOn ? 'opacity-100' : 'opacity-0'}`}
          style={{ top: tip.top, left: tip.left + 36, height: tip.height }}
        >
          <span className="truncate max-w-[200px]">{name}</span>
        </div>,
        overlayRoot,
      )}
    </button>
  )
}

/**
 * PinnedCrewRail — the pinned crews rendered as single-click icon tiles for the
 * desktop nav rail, directly under the app icon. Pinning a crew in the switcher
 * menu already records it; this surfaces that pin as a one-click switch target
 * on the rail so a frequent switcher never opens the dropdown.
 *
 * Local is deliberately NOT tiled here even when pinned: the rail's app icon
 * (the crew identity switcher header) already IS the local dashboard, so a
 * second Local tile would be the same destination twice. The rail therefore
 * shows only pinned REMOTE crews.
 *
 * TWO REALMS, like the switcher itself:
 *  - Top-level window: pins come from this realm's `useCrewPins` store, the crew
 *    list from the ['instances'] query, and a click switches via
 *    `useSelectInstance` — the switcher's own code path, so the two never drift.
 *  - Embedded remote pane: a remote pane is a separate cross-origin iframe with
 *    its OWN localStorage, so the local pin store is empty there and the
 *    instances poll is disabled. It instead reads the pin set and crew list the
 *    parent RELAYS through `instances.host` (`mc-host-model`), exactly as
 *    `EmbeddedInstanceTabBar` does, and switches by posting `mc-switch-instance`
 *    up. Without this the pins silently vanished the moment you switched INTO a
 *    remote crew — the pane saw an empty local store.
 *
 * Renders nothing when no remote crew is pinned.
 */
export function PinnedCrewRail({ orientation = 'horizontal' }: { orientation?: 'horizontal' | 'vertical' } = {}) {
  // isEmbeddedPane() is a stable per-realm fact (window.self !== window.top),
  // not reactive state, so branching the whole component on it does not violate
  // rules-of-hooks: a given realm always renders the same branch for its life.
  return isEmbeddedPane()
    ? <EmbeddedPinnedCrewRail orientation={orientation} />
    : <LocalPinnedCrewRail orientation={orientation} />
}

/** Top-level window: pins + crew list + switch all from this realm's own stores. */
function LocalPinnedCrewRail({ orientation }: { orientation: 'horizontal' | 'vertical' }) {
  const activeId = useAppSelector(s => s.instances.activeId)
  const warm = useAppSelector(s => s.instances.warm)
  const [pinned] = useCrewPins()
  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
  })
  const instances = useMemo(
    () => instancesQuery.data?.instances ?? [],
    [instancesQuery.data?.instances],
  )
  const tabInstances = useMemo(() => visibleInstanceTabs(instances, warm), [instances, warm])
  const { selectInstance, connectMutation } = useSelectInstance(instances)

  // Only pinned REMOTE crews (Local is the app icon above), in tab order so the
  // rail matches the switcher rather than reshuffling.
  const pinnedCrews = useMemo(
    () => tabInstances.filter(inst => pinned.has(inst.id)),
    [tabInstances, pinned],
  )

  const tiles = useMemo<PinnedRailTile[]>(() => {
    const list: PinnedRailTile[] = []
    // Local shows as a pinned tile when the user pinned it — including when Local
    // The ACTIVE crew is already shown at the top of the rail, so it is excluded
    // here — otherwise it would appear in two places at once. Local when it is
    // active (activeId == null) is skipped; a remote when it is active is skipped.
    if (pinned.has(LOCAL_VALUE) && activeId != null) {
      list.push({
        id: null,
        name: i18nT('components.instanceTabBar.local'),
        title: i18nT('components.instanceTabBar.switch_to_local'),
        state: 'connected',
        connecting: false,
        active: false,
      })
    }
    for (const inst of pinnedCrews) {
      if (inst.id === activeId) continue
      const st = inst.status?.state
      const target = inst.connection_method === 'ssm' ? inst.ssm_target : inst.ssh_host
      list.push({
        id: inst.id,
        name: inst.name,
        title: i18nT('components.instanceTabBar.switch_to_crew', { name: inst.name, host: target }),
        state: st,
        connecting:
          (connectMutation.isPending && connectMutation.variables === inst.id) ||
          st === 'connecting',
        active: false,
      })
    }
    return list
  }, [pinnedCrews, pinned, activeId, connectMutation.isPending, connectMutation.variables])

  // Surface a rail-initiated connect failure: this rail runs its OWN
  // useSelectInstance, so its connectMutation error reaches no other surface
  // (InstancesViewport reads only its own). errors-use-error-notice requires it
  // be shown, not swallowed.
  const connectError = connectMutation.isError ? errMessage(connectMutation.error) : undefined

  return (
    <PinnedCrewRailView
      tiles={tiles}
      onSelect={selectInstance}
      orientation={orientation}
      connectError={connectError}
    />
  )
}

/**
 * Embedded remote pane: read the pin set and crew list the parent relays through
 * `instances.host`, and switch by posting `mc-switch-instance` up — the parent
 * owns the one shared pin set and flips the active crew. Mirrors
 * `EmbeddedInstanceTabBar`'s relay contract so a pinned crew stays pinned on the
 * rail no matter which pane is on screen.
 */
function EmbeddedPinnedCrewRail({ orientation }: { orientation: 'horizontal' | 'vertical' }) {
  const host = useAppSelector(s => s.instances.host)
  const onSelect = useCallback((id: string | null) => {
    // Same relay as EmbeddedInstanceTabBar: to the hub origin only, never '*'.
    postToHost({ type: 'mc-switch-instance', v: 1, id })
  }, [])
  const pinned = useMemo(() => new Set(host?.pinnedCrews ?? []), [host?.pinnedCrews])
  const tiles = useMemo<PinnedRailTile[]>(() => {
    const list: PinnedRailTile[] = []
    // The ACTIVE crew is shown as the rail header already, so exclude it here to
    // avoid showing it twice. Inside a remote pane the header IS a remote crew,
    // so Local shows UNLESS Local is itself the active one.
    if (pinned.has(LOCAL_VALUE) && host?.activeId != null) {
      const localName = i18nT('components.instanceTabBar.local')
      list.push({
        id: null,
        name: localName,
        title: i18nT('components.instanceTabBar.switch_to_local'),
        state: 'connected',
        connecting: false,
        active: false,
      })
    }
    for (const t of host?.tabs ?? []) {
      if (!pinned.has(t.id) || t.id === host?.activeId) continue
      list.push({
        id: t.id,
        name: t.name,
        title: i18nT('components.instanceTabBar.switch_to_crew', { name: t.name, host: t.sshHost }),
        state: t.state,
        connecting: t.state === 'connecting',
        active: false,
      })
    }
    return list
  }, [host?.tabs, host?.activeId, pinned])
  return <PinnedCrewRailView tiles={tiles} onSelect={onSelect} orientation={orientation} />
}

/** Normalized tile the presenter renders, realm-agnostic. `null` id = Local. */
interface PinnedRailTile {
  id: string | null
  name: string
  title: string
  state?: string
  connecting: boolean
  active: boolean
}

/** The pinned-crew cluster — one icon tile per pinned crew. Renders nothing when
 *  none is pinned, so the surface it sits on is unchanged until a crew is pinned.
 *
 *  Two orientations:
 *   - 'horizontal' (top bar, beside the Back/Forward arrows): a row, with a single
 *     leading vertical divider separating the tiles from the nav arrows.
 *   - 'vertical' (nav rail): a column, fenced above and below by a hairline so the
 *     cluster reads as its own band between the app icon and the primary nav rows. */
function PinnedCrewRailView({
  tiles,
  onSelect,
  orientation,
  connectError,
}: {
  tiles: PinnedRailTile[]
  onSelect: (id: string | null) => void
  orientation: 'horizontal' | 'vertical'
  /** Message when a rail-initiated connect FAILS. Surfaced here because the
   *  rail runs its own useSelectInstance, so its connect error reaches no other
   *  surface — a silent failure violates errors-use-error-notice. */
  connectError?: string
}) {
  if (tiles.length === 0 && !connectError) return null
  const horizontal = orientation === 'horizontal'
  if (horizontal) {
    return (
      <div
        role="group"
        aria-label={i18nT('components.instanceTabBar.pinned_crews')}
        data-testid="rail-pinned-crews"
        className="shrink-0 flex items-center flex-row gap-1 pl-1"
      >
        {/* Leading vertical rule separating the tiles from the Back/Forward arrows. */}
        <div aria-hidden className="w-px h-5 bg-border mr-0.5 shrink-0" />
        {tiles.map(t => (
          <PinnedCrewTile
            key={t.id ?? LOCAL_VALUE}
            id={t.id}
            name={t.name}
            title={t.title}
            state={t.state}
            connecting={t.connecting}
            active={t.active}
            onSelect={() => onSelect(t.id)}
          />
        ))}
        {connectError && <RailListError message={connectError} />}
      </div>
    )
  }
  return <VerticalPinnedCrewRail tiles={tiles} onSelect={onSelect} connectError={connectError} />
}

// Collapsed, the cluster shows at most two compact tiles; the rest reveal in a
// hover FLYOUT rather than growing the column (the nav rail is overflow-hidden,
// so an in-place grow is clipped at the rail edge) and rather than scrolling (a
// scrollbar on a 32px rail is not usable).
const RAIL_VISIBLE_TILES = 3


/**
 * The vertical (nav-rail) pinned-crew cluster. Shows up to two compact tiles in
 * the rail; when more crews are pinned, hovering (or focusing into) the cluster
 * opens a small FLYOUT with every pinned crew. The flyout is portaled to
 * the overlay root and `fixed`-positioned against the cluster's own rect, so it
 * overlays beside the rail instead of being clipped by the rail's
 * `overflow-hidden` — the same escape the nav tooltips use. A down-chevron hints
 * the extra crews while collapsed.
 */
function VerticalPinnedCrewRail({
  tiles,
  onSelect,
  connectError,
}: {
  tiles: PinnedRailTile[]
  onSelect: (id: string | null) => void
  connectError?: string
}) {
  const overlayRoot = useOverlayRoot()
  const [open, setOpen] = useState(false)
  // Anchored to the TILE COLUMN (not the whole cluster) so the flyout aligns to
  // the first tile's exact box, with no leading hairline/padding in between.
  const columnRef = useRef<HTMLDivElement>(null)
  const [rect, setRect] = useState<{ top: number; left: number; width: number } | null>(null)
  const overflow = tiles.length > RAIL_VISIBLE_TILES
  const visible = tiles.slice(0, RAIL_VISIBLE_TILES)

  // The flyout's first tile is inset from the flyout's own top-left by its
  // padding (p-1 = 4px) plus its 1px border. Shifting the flyout up-and-left by
  // that inset makes its first tile land PIXEL-EXACT on the in-rail first tile,
  // so hovering a visible icon does not nudge it at all.
  const FLYOUT_INSET_PX = 5

  // Measure the tile column's position so the fixed flyout anchors to it.
  // Remeasure on open and, while open, on scroll/resize — the nav-tooltip pattern.
  const measure = useCallback(() => {
    const el = columnRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    setRect({ top: r.top, left: r.left, width: r.width })
  }, [])
  useEffect(() => {
    if (!open) return
    measure()
    window.addEventListener('scroll', measure, true)
    window.addEventListener('resize', measure)
    return () => {
      window.removeEventListener('scroll', measure, true)
      window.removeEventListener('resize', measure)
    }
  }, [open, measure])

  const flyoutOpen = overflow && open
  const renderTile = (t: PinnedRailTile) => (
    <PinnedCrewTile
      key={t.id ?? LOCAL_VALUE}
      id={t.id}
      name={t.name}
      title={t.title}
      state={t.state}
      connecting={t.connecting}
      active={t.active}
      onSelect={() => onSelect(t.id)}
    />
  )

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- hover/focus REVEAL only, no action on this wrapper: every crew inside is a real focusable button, and focus-within opens the flyout so a keyboard-reached crew is visible. There is no gesture here to mirror with a keyboard handler.
    <div
      data-testid="rail-pinned-crews"
      className="shrink-0 flex flex-col items-center -mt-1 pb-2 mb-1 border-b border-border"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      {/* In-rail column: the first two tiles (or all, when ≤2 are pinned). This
          block NEVER changes size on hover — the full set appears in the flyout
          overlay, not by growing this column — so nothing below the rail shifts. */}
      <div
        ref={columnRef}
        role="group"
        aria-label={i18nT('components.instanceTabBar.pinned_crews')}
        className="flex flex-col items-center gap-1.5"
      >
        {(overflow ? visible : tiles).map(renderTile)}
      </div>
      {/* A rail-initiated connect failure is surfaced here as always-on visible
          text + Ask-the-agent hand-off (errors-use-error-notice): the rail owns
          its own useSelectInstance, so this connect error reaches no other
          surface. */}
      {connectError && <RailListError message={connectError} />}
      {/* The only divider is this section's own bottom border, between the pinned
          crews and the primary nav rows below — matching the bottom rail's
          community/github divider spacing. There is no top divider: the app icon
          above already reads as its own thing. */}

      {/* Hover flyout: every pinned crew, portaled out of the rail so the rail's
          overflow-hidden cannot clip it. Fixed-positioned over the cluster's own
          column (same left + top) so the tiles sit where the in-rail ones were,
          just un-clipped and complete. No enter/exit animation — it appears and
          disappears instantly, which is what avoids the flicker a fade produced. */}
      {flyoutOpen && rect && createPortal(
        // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- the handlers keep the flyout open while the pointer is inside it (a hover bridge); every crew in it is a real focusable button, so there is no container gesture needing a keyboard equivalent.
        <div
          data-testid="rail-pinned-crews-flyout"
          className="fixed z-[9999] flex flex-col items-center gap-1.5 p-1 rounded-[10px] bg-card border border-border shadow-lg"
          // Shifted up-and-left by the flyout's own inset (padding + border) so
          // its first tile sits exactly on top of the in-rail first tile — the
          // visible icons do not move when the flyout appears.
          style={{ top: rect.top - FLYOUT_INSET_PX, left: rect.left - FLYOUT_INSET_PX }}
          onMouseEnter={() => setOpen(true)}
          onMouseLeave={() => setOpen(false)}
        >
          {/* The already-visible tiles render INSTANTLY and in place — the card
              frame appears around them with no shift. */}
          {visible.map(renderTile)}
          {/* The overflow tiles expand downward: a height 0→auto animation so the
              card grows open vertically rather than popping to full size. The
              parent flyout is a gap-1.5 flex column, so the 6px gap before this
              block is already provided by that gap — this container must NOT add
              its own top margin, or the first revealed tile sits a double gap
              (12px) below the ones above it. */}
          <motion.div
            className="flex flex-col items-center gap-1.5 overflow-hidden"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            transition={{ duration: 0.12, ease: 'easeOut' }}
          >
            {tiles.slice(RAIL_VISIBLE_TILES).map(renderTile)}
          </motion.div>
        </div>,
        overlayRoot,
      )}
    </div>
  )
}
