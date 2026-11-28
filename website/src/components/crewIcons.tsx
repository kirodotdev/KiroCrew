/**
 * Crew icons: one glyph per remote crew, so the switcher's chips and rows can be
 * told apart without reading the name. The name is the only other identifier, and
 * a phone chip draws no name, so there the icon is the whole chip. Local keeps
 * the house.
 *
 * A crew gets an icon the first time this dashboard lists it, picked at random
 * from the icons the listed crews use least, so a handful of crews never share
 * one. The pick is stored, so it survives reloads, and Settings -> Remote Crew
 * changes it (`CrewIconPicker` in `crewIconPicker.tsx`).
 *
 * Storage is the same shape as the switcher's pin set: a module-level store over
 * one localStorage key, because several bars in this realm are mounted at once
 * and must change together. A remote pane is a separate cross-origin realm, so it
 * never reads this store; the parent relays each tab's icon in `mc-host-model`.
 */
import { useSyncExternalStore } from 'react'
import {
  Anchor,
  Bird,
  Cat,
  Coffee,
  Compass,
  Crown,
  Dog,
  Feather,
  Fish,
  Flame,
  Gem,
  Leaf,
  Loader2,
  Moon,
  Mountain,
  Music,
  Rabbit,
  Snowflake,
  Star,
  Sun,
  Turtle,
  type LucideIcon,
} from 'lucide-react'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { savePrefExplicitly } from '../lib/uiPrefs'
import { stateDotCls, stateTextCls } from './crewStateMark'

/** localStorage key: a JSON object mapping instance id to an icon key. */
export const CREW_ICONS_PREF_KEY = 'mc-crew-switcher-icons'

/**
 * The icons a crew can wear, keyed by the word that is stored. None of them is a
 * glyph the switcher already uses for something else (house, lock, pin, cloud).
 */
export const CREW_ICONS = {
  star: Star,
  moon: Moon,
  sun: Sun,
  leaf: Leaf,
  flame: Flame,
  mountain: Mountain,
  anchor: Anchor,
  compass: Compass,
  feather: Feather,
  fish: Fish,
  bird: Bird,
  cat: Cat,
  dog: Dog,
  rabbit: Rabbit,
  turtle: Turtle,
  coffee: Coffee,
  gem: Gem,
  crown: Crown,
  snowflake: Snowflake,
  music: Music,
} satisfies Record<string, LucideIcon>

export type CrewIconKey = keyof typeof CREW_ICONS

export const CREW_ICON_KEYS = Object.keys(CREW_ICONS) as CrewIconKey[]

export function isCrewIconKey(value: unknown): value is CrewIconKey {
  return typeof value === 'string' && Object.hasOwn(CREW_ICONS, value)
}

export type CrewIconMap = Readonly<Record<string, CrewIconKey>>

/**
 * One crew's icon. Always read the map through this: a crew id is a slug of its
 * name, so `constructor` is a valid id, and a plain index would return the
 * inherited `Object` function for it.
 */
export function crewIconOf(map: CrewIconMap, id: string): CrewIconKey | undefined {
  return Object.hasOwn(map, id) ? map[id] : undefined
}

/** Parse the stored map. A corrupted value, or an entry naming no known icon, is dropped. */
export function parseCrewIcons(raw: string | null): CrewIconMap {
  if (raw === null) return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const out: Record<string, CrewIconKey> = {}
    for (const [id, key] of Object.entries(parsed)) if (isCrewIconKey(key)) out[id] = key
    return out
  } catch {
    return {}
  }
}

/**
 * The map with an icon for every id in `ids` that has none. Each new crew takes a
 * random icon among the ones the crews in `ids` use least, so duplicates appear
 * only once every icon is taken. Crews not in `ids` do not count: a removed
 * crew's icon is free again. Returns `current` itself when nothing was added.
 *
 * ponytail: entries for removed crews are never pruned. Each is a few bytes; prune
 * them here if a user ever carries hundreds of crews through one profile.
 */
export function assignCrewIcons(
  current: CrewIconMap,
  ids: readonly string[],
  random: () => number = Math.random,
): CrewIconMap {
  const uses = new Map<CrewIconKey, number>(CREW_ICON_KEYS.map(k => [k, 0]))
  for (const id of new Set(ids)) {
    const key = crewIconOf(current, id)
    if (key) uses.set(key, (uses.get(key) ?? 0) + 1)
  }
  let next: Record<string, CrewIconKey> | null = null
  for (const id of new Set(ids)) {
    if (crewIconOf(current, id)) continue
    const least = Math.min(...uses.values())
    const free = CREW_ICON_KEYS.filter(k => uses.get(k) === least)
    const key = free[Math.min(free.length - 1, Math.floor(random() * free.length))]
    next ??= { ...current }
    next[id] = key
    uses.set(key, least + 1)
  }
  return next ?? current
}

let iconsState: CrewIconMap = parseCrewIcons(safeGetItem(CREW_ICONS_PREF_KEY))
const listeners = new Set<() => void>()

/**
 * The stored value this tab's last write was refused over, or `undefined` once a
 * write lands. While storage still holds exactly that value, storage is older than
 * `iconsState`; any other tab's write changes it and storage is the newer side again.
 */
let refusedOver: string | null | undefined

function storedIcons(): CrewIconMap {
  return parseCrewIcons(safeGetItem(CREW_ICONS_PREF_KEY))
}

/**
 * The map as it stands now. Another tab of this origin may have written since
 * this tab last heard (its `storage` event can still be queued), so every write
 * starts here, never from this tab's copy alone: the stored entry wins, and this
 * tab's copy fills in only what storage lacks. While storage still holds what it
 * held when it refused this tab's write, it is the older side, so this tab's copy
 * wins instead; otherwise an assignment would re-roll and a pick would snap back.
 *
 * shortcut: the read and the write are one synchronous task, not a lock across
 * tabs, so two tabs writing in the same few microseconds can still drop one pick.
 * Take a Web Lock around the write if that ever shows up.
 */
function latestIcons(): CrewIconMap {
  const raw = safeGetItem(CREW_ICONS_PREF_KEY)
  const stored = parseCrewIcons(raw)
  return raw === refusedOver ? { ...stored, ...iconsState } : { ...iconsState, ...stored }
}

/**
 * Another tab of this origin wrote the map: adopt the map as it stands now
 * (`latestIcons()`), so this tab draws that tab's assignments and keeps a pick
 * storage refused after that write. Not the event's own value: a queued event can
 * carry a map older than a later write. `storage` never fires in the tab that
 * wrote, so adopting cannot loop.
 */
if (typeof window !== 'undefined') {
  window.addEventListener('storage', e => {
    if (e.storageArea && e.storageArea !== localStorage) return
    if (e.key !== CREW_ICONS_PREF_KEY || e.newValue === null) return
    iconsState = latestIcons()
    listeners.forEach(l => l())
  })
}

function publish(next: CrewIconMap, explicit: boolean) {
  iconsState = next
  const raw = JSON.stringify(next)
  // An assignment is a default this dashboard picked; a pick in Settings is the
  // user's own choice, which the host backup must not put a stale value over.
  const saved = explicit ? savePrefExplicitly(CREW_ICONS_PREF_KEY, raw) : safeSetItem(CREW_ICONS_PREF_KEY, raw)
  refusedOver = saved ? undefined : safeGetItem(CREW_ICONS_PREF_KEY)
  listeners.forEach(l => l())
}

/** Give every listed crew an icon it does not have yet. A no-op when all have one. */
export function ensureCrewIcons(ids: readonly string[], random?: () => number) {
  const base = latestIcons()
  const next = assignCrewIcons(base, ids, random)
  if (next !== base) publish(next, false)
}

/** The user's pick for one crew. */
export function setCrewIcon(id: string, key: CrewIconKey) {
  const base = latestIcons()
  if (crewIconOf(base, id) === key) return
  publish({ ...base, [id]: key }, true)
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

/** Re-read the stored map, as a page load does. Tests only: the store reads at import. */
export function __reloadCrewIconsForTests() {
  refusedOver = undefined
  iconsState = storedIcons()
  listeners.forEach(l => l())
}

/** Reactive read of every crew's icon. */
export function useCrewIcons(): CrewIconMap {
  return useSyncExternalStore(subscribe, () => iconsState, () => iconsState)
}

/** The crew's icon alone, sized to sit in the phone chip's badge disc. */
export function CrewIconInBadge({ icon }: { icon: CrewIconKey }) {
  const Icon = CREW_ICONS[icon]
  return <Icon className="w-2.5 h-2.5" aria-hidden data-crew-icon={icon} />
}

/**
 * The mark's shape for a tunnel state, so the state never rests on colour alone
 * in the switcher's rows and desktop chips: a filled dot while live, a diamond for
 * an error, and a hollow ring when stopped or disconnected. The same rule as the
 * badge's outline styles (`crewStateMark.tsx`), which a phone chip uses instead,
 * in shapes that still read at 6px.
 */
export function markShape(state?: string): 'dot' | 'diamond' | 'ring' {
  return state === 'connected' ? 'dot' : state === 'error' ? 'diamond' : 'ring'
}

const MARK_SHAPE_CLS: Record<ReturnType<typeof markShape>, (state?: string) => string> = {
  dot: state => `rounded-full ${stateDotCls(state)}`,
  diamond: state => `rotate-45 rounded-[1px] ${stateDotCls(state)}`,
  ring: state => `rounded-full border-[1.5px] border-current bg-[var(--bg)] ${stateTextCls(state)}`,
}

/**
 * A remote crew's glyph with its connection state as a mark on the corner. With
 * no icon (a Relay Crew tab, or a pane whose host relays none) the mark is the
 * whole glyph, centred. While the tunnel connects the spinner stands in for the
 * glyph, as it does for the letter badge. Every form is 1em square, so a chip's
 * width does not depend on which one it shows.
 */
export function CrewGlyph({
  icon,
  state,
  connecting = false,
}: {
  icon?: CrewIconKey
  state?: string
  connecting?: boolean
}) {
  if (connecting) return <Loader2 className="lucide-inline shrink-0 animate-spin" aria-hidden />
  const Icon = icon ? CREW_ICONS[icon] : null
  const shape = markShape(state)
  return (
    <span className="lucide-inline relative" aria-hidden data-crew-icon={icon}>
      {Icon ? <Icon className="block w-full h-full" /> : null}
      <span
        data-testid="crew-state-mark"
        data-state={state ?? ''}
        data-shape={shape}
        className={
          'absolute w-1.5 h-1.5 ' +
          (Icon ? '-right-0.5 -bottom-0.5 ring-[1.5px] ring-[var(--bg)] ' : 'inset-0 m-auto ') +
          MARK_SHAPE_CLS[shape](state)
        }
      />
    </span>
  )
}
