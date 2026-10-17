/**
 * petBridge — the pet's `api` object, split by transport.
 *
 * The pet needs two very different kinds of call, and conflating them was a
 * real bug during the port (everything was pointed at panelBridge, which is
 * HTTP/WS):
 *
 *  - SHELL capabilities — `updateHitbox`, `savePosition`, drag handoff, display
 *    geometry. These manipulate the Electron window itself and can ONLY go over
 *    IPC. They are exposed by the pet window's preload as `window.mochi` and
 *    are simply ABSENT in a browser tab.
 *  - DATA — watch list, pinned files, stats, chat. Same-origin HTTP/WS, exactly
 *    as the panel uses.
 *
 * Every shell method is forwarded undefined-safe, so the original call sites
 * (`api?.updateHitbox?.(...)`) degrade to a no-op in a browser tab instead of
 * crashing. That is what makes `mochi-pet.html` usable as a dev preview: the
 * sprite, animation, mood and bubble work all run, while click-through and
 * position persistence — which are meaningless without the overlay window —
 * quietly do nothing.
 *
 * IMPORTANT (hitbox): `updateHitbox` is the one shell method the pet cannot
 * really live without in Electron. It drives `setIgnoreMouseEvents`, so if it
 * is missing the full-screen overlay swallows every click on the desktop
 * beneath it. The pet window's preload MUST provide it.
 */

/** Provided by the pet window's preload; undefined in a plain browser tab. */
const shell: Record<string, ((...args: unknown[]) => unknown) | undefined> | undefined = (
  window as unknown as {
    mochi?: Record<string, ((...args: unknown[]) => unknown) | undefined>
  }
).mochi

/** Event types published by the Python runtime (see app.json permissions.events). */
const MOVE_TYPE = 'mochi:move'
const NOTIFY_TYPE = 'mochi:notify'

/** True when running inside the Electron pet window (shell IPC available). */
export const hasShell = shell !== undefined

/**
 * Forward a shell method, tolerating its absence.
 *
 * Returns `undefined` rather than throwing so a listener-style method
 * (`onWalk(cb)`) yields an undefined unsubscribe, which the ported cleanup code
 * already calls as `off?.()`.
 */
/**
 * Warn ONCE per missing method, in dev only.
 *
 * WHY THIS EXISTS: optional chaining makes "the preload never exposed this
 * method" indistinguishable from "the method ran and returned undefined". Four
 * separate features shipped broken behind that ambiguity — `quitApp` (a menu
 * item that did nothing), `contextMenuAction` (an entire relay with no
 * main-process handler), `getBackendStatus` (a permanent "disconnected" banner),
 * and `getRoster` (a strip that only ever showed its empty state). None of them
 * threw; they just quietly did not exist, and each was found by a human noticing
 * the UI was wrong.
 *
 * A missing SHELL is normal (browser preview, dev without Electron) and stays
 * silent. A missing METHOD while a shell IS present is a real wiring gap and is
 * what this reports.
 *
 * Dev-only and once-per-name: a method called from a render path or a 16ms poll
 * would otherwise flood the console and become noise nobody reads.
 */
const warned = new Set<string>()

function warnMissing(name: string): void {
  if (!import.meta.env.DEV) return
  if (shell === undefined) return // no shell at all — expected outside Electron
  if (warned.has(name)) return
  warned.add(name)
  // eslint-disable-next-line no-console
  console.warn(
    `[mochi] shell method "${name}" is not exposed by the pet preload — ` +
      'the call was swallowed. Add it to pet-preload.js, or remove the caller.',
  )
}

function fwd<T = unknown>(name: string) {
  return (...args: unknown[]): T | undefined => {
    const fn = shell?.[name]
    if (fn === undefined) {
      warnMissing(name)
      return undefined
    }
    return fn(...args) as T | undefined
  }
}

/**
 * Forward a method the callers `.then()` on.
 *
 * `fwd` is not safe for these: the ported call sites read
 * `api?.getPetState?.().then(...)` — optional chaining guards the CALL but not
 * the `.then` on its result, so returning undefined throws
 * "Cannot read properties of undefined (reading 'then')" and takes the whole
 * pet render down with it. Resolving to undefined instead keeps those call
 * sites' own `if (s)` / `if (c)` guards in charge.
 */
function fwdAsync<T = unknown>(name: string) {
  return async (...args: unknown[]): Promise<T | undefined> => {
    const fn = shell?.[name]
    if (fn === undefined) {
      warnMissing(name)
      return undefined
    }
    return (await fn(...args)) as T | undefined
  }
}

// ── Window / overlay geometry ───────────────────────────────────────────────
export const updateHitbox = fwd('updateHitbox')
/** Falls back to the origin so callers do not have to handle a missing shell. */
export async function getWindowPosition(): Promise<{ x: number; y: number }> {
  const pos = await fwdAsync<{ x: number; y: number }>('getWindowPosition')()
  return pos ?? { x: 0, y: 0 }
}
export const savePosition = fwd('savePosition')
export const onDisplaysInfo = fwd<() => void>('onDisplaysInfo')
export const onSetActive = fwd<() => void>('onSetActive')
export const onHide = fwd<() => void>('onHide')

// ── Drag handoff (the shell owns the mouse while dragging) ──────────────────
export const dragStart = fwd('dragStart')
export const dragEnd = fwd('dragEnd')
export const dragMouseup = fwd('dragMouseup')
export const onDragUpdate = fwd<() => void>('onDragUpdate')
export const onDragEnded = fwd<() => void>('onDragEnded')
export const onDragListenMouseup = fwd<() => void>('onDragListenMouseup')

// ── Walking, bubbles, peeking: the GATEWAY event bus, not shell IPC ─────────
//
// Upstream these were shell IPC because the intent originated in ITS main
// process. Here the intent originates in Python: `perform_pet_action` queues a
// task, the runtime's queue poller executes it and publishes `mochi:move` /
// `mochi:notify` on the app event bus. The shell cannot subscribe to that bus,
// so these ride the socket the pet page already has open.
//
// The walk GEOMETRY moved with them (ported from the original main/index.ts
// perform_pet_action handler). It needs the active display's work area — hence
// the `workArea` field added to the displays-info payload. Everything else is
// the original's arithmetic, including the 20px dead-band and the
// PET_BOTTOM_MARGIN floor that keeps the pet off the Dock.

type WalkPoint = { x: number; y: number }

const walkListeners = new Set<(x: number, y: number) => void>()
const walkPathListeners = new Set<(points: WalkPoint[]) => void>()
const walkAppendListeners = new Set<(points: WalkPoint[]) => void>()
const walkCancelListeners = new Set<() => void>()
const bubbleListeners = new Set<(text: string, sticky: boolean) => void>()

function addTo<T>(set: Set<T>, cb: T): () => void {
  set.add(cb)
  return () => {
    set.delete(cb)
  }
}

/** Active display work area, from the shell's displays-info + set-active. */
let myWorkArea: { width: number; height: number } | null = null
/** Last known pet position, needed for the dead-band and edge behaviours. */
let lastPos: WalkPoint = { x: 0, y: 0 }

if (shell !== undefined) {
  const onInfo = shell.onDisplaysInfo as
    | ((cb: (displays: unknown[], myId: number, activeId?: number) => void) => void)
    | undefined
  onInfo?.((displays, myId, activeId) => {
    const mine = (displays as { id: number; workArea?: { width: number; height: number } }[]).find(
      (d) => d.id === myId,
    )
    if (mine?.workArea !== undefined) myWorkArea = mine.workArea
    // Report the display list to the backend so the pet-action tool's "query"
    // can answer it — the shell knows the monitors, Python does not.
    //
    // `activeId` comes from the SHELL, never from `myId`. This event is
    // broadcast to every overlay, so posting one's own display as the active one
    // made the cache a race: the pet reported whichever monitor's window POSTed
    // last, which is why it insisted it was on a screen it was not. Every
    // overlay now posts the same answer, so a last-writer-wins cache is correct.
    void fetch('/api/apps/mochi/displays', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ displays, activeId: activeId ?? myId }),
    }).catch(() => undefined)
  })
  const onActive = shell.onSetActive as
    | ((cb: (active: boolean, x?: number, y?: number) => void) => void)
    | undefined
  onActive?.((active, x, y) => {
    if (active && typeof x === 'number' && typeof y === 'number') lastPos = { x, y }
  })
  const onEnded = shell.onDragEnded as
    | ((cb: (x: number, y: number) => void) => void)
    | undefined
  onEnded?.((x, y) => {
    lastPos = { x, y }
    reportStat('drag')
  })
}

/** Straight-line duration, matching the original's `calcDur`. */
function clampWalk(p: WalkPoint, wa: { width: number; height: number }): WalkPoint {
  const maxY = wa.height - PET_H - PET_BOTTOM_MARGIN
  return {
    x: Math.max(0, Math.min(wa.width - PET_W, p.x)),
    y: Math.max(0, Math.min(maxY, p.y)),
  }
}

interface MoveAction {
  x?: number
  y?: number
  waypoints?: WalkPoint[]
  behavior?: 'hide_left' | 'hide_right' | 'return'
  interrupt?: boolean
  display?: number
}

/** Port of the original perform_pet_action move branch. */
export function handleMove(action: MoveAction): void {
  // A browser tab has no overlay, so fall back to the viewport; the pet still
  // walks, which is what makes mochi-pet.html usable as a dev preview.
  const wa = myWorkArea ?? { width: window.innerWidth, height: window.innerHeight }

  if (action.display !== undefined && action.display !== null) {
    // Move to another monitor first, centring unless a point was given.
    const transfer = shell?.transferToDisplay as
      | ((id: number, x: number, y: number) => void)
      | undefined
    transfer?.(
      action.display,
      action.x ?? Math.floor(wa.width / 2 - PET_W / 2),
      action.y ?? Math.floor(wa.height / 2 - PET_H / 2),
    )
  }

  const isQuery =
    action.waypoints === undefined &&
    action.behavior === undefined &&
    action.x === undefined &&
    action.y === undefined
  if (isQuery) return

  // `interrupt: false` APPENDS to the walk in flight; anything else replaces it.
  if (action.interrupt !== false) {
    for (const cb of walkCancelListeners) cb()
  }

  if (action.waypoints !== undefined && action.waypoints.length > 0) {
    const clamped = action.waypoints.map((p) => clampWalk(p, wa))
    lastPos = clamped[clamped.length - 1]
    const set = action.interrupt === false ? walkAppendListeners : walkPathListeners
    for (const cb of set) cb(clamped)
    return
  }

  if (action.behavior === 'hide_left') {
    lastPos = { x: 0, y: lastPos.y }
    for (const cb of walkListeners) cb(0, lastPos.y)
    return
  }
  if (action.behavior === 'hide_right') {
    lastPos = { x: wa.width - PET_W, y: lastPos.y }
    for (const cb of walkListeners) cb(lastPos.x, lastPos.y)
    return
  }
  if (action.behavior === 'return') {
    lastPos = {
      x: Math.floor(wa.width / 2),
      y: Math.floor((wa.height - PET_BOTTOM_MARGIN) / 2),
    }
    for (const cb of walkListeners) cb(lastPos.x, lastPos.y)
    return
  }

  if (action.x !== undefined && action.y !== undefined && action.display === undefined) {
    const target = clampWalk({ x: action.x, y: action.y }, wa)
    // Dead-band: upstream ignores a move under 20px of Manhattan distance so a
    // planner nudging the pet by a few pixels does not trigger a walk animation.
    const dist = Math.abs(target.x - lastPos.x) + Math.abs(target.y - lastPos.y)
    if (dist <= 20) return
    lastPos = target
    for (const cb of walkListeners) cb(target.x, target.y)
  }
}

subscribeAppEvent(MOVE_TYPE, (payload) => handleMove((payload ?? {}) as MoveAction))

subscribeAppEvent(NOTIFY_TYPE, (payload) => {
  const action = (payload ?? {}) as { summary?: string; sticky?: boolean }
  const text = typeof action.summary === 'string' ? action.summary : ''
  if (text === '') return // nothing to show; do not flash an empty bubble
  for (const cb of bubbleListeners) cb(text, action.sticky === true)
})

export const onWalk = (cb: (x: number, y: number) => void) => addTo(walkListeners, cb)
export const onWalkPath = (cb: (points: WalkPoint[]) => void) => addTo(walkPathListeners, cb)
export const onWalkAppend = (cb: (points: WalkPoint[]) => void) => addTo(walkAppendListeners, cb)
export const onWalkCancel = (cb: () => void) => addTo(walkCancelListeners, cb)

/** Post-walk reports go to the backend: it owns pet state and the stats file. */
function report(path: string, body: Record<string, unknown>): void {
  void fetch(`/api/apps/mochi/${path}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => undefined)
}

export function walkDone(): void {
  report('walk-done', {})
}

/** Actual pixels walked — may be partial when a walk was interrupted. */
export function reportWalkDistance(pixels: number): void {
  if (typeof pixels !== 'number' || pixels <= 0) return
  report('walk-distance', { pixels })
}

export const onPlayMotion = fwd<() => void>('onPlayMotion')

/** Test seam: inject the work area the shell would have reported. */
export function _setWorkAreaForTest(wa: { width: number; height: number } | null): void {
  myWorkArea = wa
}

/** Test seam: the position the dead-band and edge behaviours read. */
export function _setLastPosForTest(pos: { x: number; y: number }): void {
  lastPos = pos
}

/** The pet tucked itself against an edge (or came back). Backend records the
 *  stat and re-broadcasts so the panel sees it too. */
export function setPeeking(peeking: boolean): void {
  report('peeking', { peeking })
}

// ── Pet state / mood ─────────────────────────────────────────────────────────
//
// State and mood are backend-owned now (PetStateManager) and travel over
// same-origin HTTP + the dashboard WS, NOT shell IPC — the pet window's preload
// never exposed getPetState/onStateChange/onMood, so declaring them as shell
// forwards silently swallowed the pet's whole state/mood read path (title stuck
// on 'offline', mood never applied). getPetState/onStateChange/onMood are
// re-exported from panelBridge at the bottom of this file.

// ── Bubbles ─────────────────────────────────────────────────────────────────
//
// Delivered over the event bus (see handleMove's neighbours above): the agent's
// notify action reaches the runtime, which publishes `mochi:notify`. Upstream
// queued bubbles while every window was hidden; the builtin's hide path lives in
// the shell and the pet simply stops rendering, so there is nothing to queue —
// see MOCHI_MIGRATION.md item 25.
export const onBubble = (cb: (text: string, sticky: boolean) => void) => addTo(bubbleListeners, cb)
export const onBubbleForceDismiss = fwd<() => void>('onBubbleForceDismiss')
/** Dismissal is renderer-local (useBubble owns the timer); nothing to report. */
export function dismissBubble(): void {}

// ── Appearance / config ─────────────────────────────────────────────────────
//
// These are HTTP, not shell IPC. The original read them over Electron IPC from a
// config file the standalone app owned; the port initially declared them as
// shell forwards, but the preload never exposed any of them — so the pet's whole
// appearance-read path was swallowed by optional chaining and the pet silently
// ran on its compiled-in default art no matter what the user picked.
//
// Now they read the gateway: settings for the active pack, and the pack routes
// for its manifest (see appearance_store.py).

/** The subset of Mochi settings the pet reacts to. */
export interface PetConfig {
  activeAppearance: string
  catPreset: string | null
  /** Panel/pet UI language; '' follows the browser locale. */
  language?: string
  petName?: string
}

export async function getMochiConfig(): Promise<PetConfig | undefined> {
  try {
    const res = await fetch('/api/apps/mochi/settings', { credentials: 'same-origin' })
    if (!res.ok) return undefined
    return (await res.json()) as PetConfig
  } catch {
    // The pet must keep rendering if the read fails; callers fall back to
    // built-in art.
    return undefined
  }
}

export async function galleryGetPackDetail(packId: string): Promise<PackManifest | undefined> {
  try {
    const res = await fetch(`/api/apps/mochi/packs/${encodeURIComponent(packId)}`, {
      credentials: 'same-origin',
    })
    return res.ok ? await res.json() : undefined
  } catch {
    return undefined
  }
}

/** URL of one image inside a pack — usable directly as an `<img src>`. */
export function galleryPackFileUrl(packId: string, filename: string): string {
  return `/api/apps/mochi/packs/${encodeURIComponent(packId)}/file/${encodeURIComponent(filename)}`
}

/**
 * One subscription to the settings-changed broadcast, shared by the three
 * appearance events below so a single frame does not fan out into three sockets.
 */
function onAppearanceSettings(
  cb: (settings: Record<string, unknown>) => void | Promise<void>,
): () => void {
  return onColorMapSettings((payload) => {
    void cb((payload ?? {}) as Record<string, unknown>)
  })
}

/**
 * Appearance change notifications — WS, not shell IPC.
 * These were declared as shell forwards, but the preload never exposed any of
 * them, so every appearance change was invisible until the pet was reopened.
 * The backend now publishes `mochi:color-map-changed` (payload: the new
 * settings) and `mochi:gallery-packs-changed` on the same socket the pet already
 * uses for state and mood.
 */
export function onConfigUpdated(cb: (cfg: Record<string, unknown>) => void): () => void {
  return onAppearanceSettings((settings) => cb(settings))
}

/**
 * Only one theme exists now (the pet follows KiroCrew's), so there is nothing to
 * push. Kept as a real no-op subscription rather than a missing method, so the
 * ported call site is honest instead of silently swallowed.
 */
export function onThemeChanged(_cb: (themeId: string) => void): () => void {
  return () => {}
}

/** `{packId, colorMap}` — the shape the pet's renderer already consumes. */
export function onColorMapChanged(
  cb: (data: { packId: string; colorMap: Record<string, string> }) => void,
): () => void {
  return onAppearanceSettings(async (settings) => {
    // Only the cat has a colour map, so the consumer's `packId ===
    // 'default-mochi'` test must go FALSE while another pack is active. Going
    // through the shared resolver keeps that true for the ghost too.
    const packId = resolveActivePackId(settings)
    const colorMap = (await presetsGetColorMap()) ?? {}
    cb({ packId, colorMap })
  })
}

/**
 * Active pack switched. Non-default packs need their manifest before the pet can
 * rebuild its animation resolver, so the detail is fetched here and merged into
 * the payload — the ported consumer keys off `meta`/`animations`, which is why
 * both this path and the mount path must go through the SAME builder.
 *
 * A recolour is not a switch: the emit is deduped on the resolved pack id, so the
 * settings event fires for both and only a real change rebuilds the resolver.
 */
export function onGalleryActiveChanged(
  cb: (data: Record<string, unknown>) => void,
): () => void {
  let lastPackId: string | null = null
  let stopped = false

  const emit = async (settings: Record<string, unknown>): Promise<void> => {
    if (stopped) return
    const packId = resolveActivePackId(settings)
    if (packId === lastPackId) return // a recolour is not a pack switch
    lastPackId = packId
    if (packId === BUILTIN_MOCHI_ID) {
      // The cat alone: the pet holds its SVGs compiled in and recolours them,
      // so it rebuilds from those rather than from a detail payload.
      cb({ packId })
      return
    }
    // A built-in's art ships in this bundle, so its detail is produced locally;
    // a user pack is read from the packs route and its art INLINED. Both go
    // through the one shared builder: this path used to call the raw-manifest
    // reader, whose payload carries FILENAMES and no `animations` key at all —
    // and PetWidget's handler is written `if (data?.meta && data?.animations)`,
    // so every live switch to a user pack was a silent no-op. The pet kept the
    // art it already had (the compiled-in cat whenever no resolver was set), and
    // only a restart — which goes through the seam's flattening builder — showed
    // the pack the user picked.
    const detail = (await resolvePackDetail(packId)) as Record<string, unknown> | null
    if (detail === null) {
      // eslint-disable-next-line no-console
      console.error('[mochi-pet] active pack has no readable detail', packId)
    }
    cb({ packId, ...(detail ?? {}) })
  }

  const offEvent = onAppearanceSettings((settings) => {
    void emit(settings)
  })

  return () => {
    stopped = true
    offEvent()
  }
}

/**
 * The cat colourway for the built-in cat pack.
 *
 * Resolved from the stored `catPreset` id against the built-in preset table,
 * rather than from a shell method that does not exist.
 */
/**
 * Colour map for a preset pack. `packId` is accepted (and currently unused)
 * because every vendored call site passes `'default-mochi'` — the built-in cat
 * is the only pack that carries presets.
 */
export async function presetsGetColorMap(
  packId?: string,
): Promise<Record<string, string> | undefined> {
  const cfg = (await getMochiConfig()) as (PetConfig & {
    colorMaps?: Record<string, Record<string, string>>
  }) | undefined
  if (cfg === undefined) return undefined

  // A per-pack map wins: it is what the Avatars window's colour customiser
  // writes, and it used to be ignored entirely here — so a recolour persisted
  // and then never showed up on the pet.
  const target = packId ?? cfg.activeAppearance ?? ""
  const perPack = cfg.colorMaps?.[target] ?? cfg.colorMaps?.["default-mochi"]
  if (perPack !== undefined && Object.keys(perPack).length > 0) return perPack

  // Otherwise fall back to the built-in coat the user picked.
  if (!cfg.catPreset) return undefined
  const { BUILT_IN_CAT_PRESETS } = await import('../src/shared/builtInCatPresets')
  return BUILT_IN_CAT_PRESETS.find((p) => p.id === cfg.catPreset)?.colorMap
}

// ── User actions ────────────────────────────────────────────────────────────
//
// DECISION — each pet right-click action gets its OWN named shell method
// (openChat / openAvatars / openMemories / openSettings), and the generic
// `contextMenuAction(name)` relay is RETIRED.
//
// That relay is what silently rotted every menu item: the renderer forwarded an
// action STRING to `window.mochi.contextMenuAction`, but the preload and main
// process never had a channel for it, so each item was a no-op with no error
// anywhere. Beyond being broken, a string-keyed renderer→main relay is a WIDER
// surface than enumerated channels — it is precisely the "pass-through
// invoke(channel, …)" the pet preload's own header forbids, since page content
// could name any action. Named methods keep the IPC surface explicit and
// auditable, and turn a missing handler into a visible `undefined` instead of a
// dead menu item. `avatars` already set this precedent (openAvatars →
// `mochi-avatar:open`); the rest now follow it.
export const openChat = fwd('openChat')
export const openAvatars = fwd('openAvatars')
// Memories and Settings are panel-hosted views. The panel is a DIFFERENT window
// from the pet overlay, so these ask the shell to open/focus the panel and
// switch it to the view (see pet-preload + panelWindow.showPanelView) rather
// than trying to flip renderer state the pet window does not own.
export const openMemories = fwd('openMemories')
export const openSettings = fwd('openSettings')
// Shared-menu rows implemented by the panel: the shell opens/focuses the
// panel and asks it to run its own handler (see panel/mochiMenu.ts).
export const clearScreenInPanel = fwd('clearScreenInPanel')
export const deleteHistoryInPanel = fwd('deleteHistoryInPanel')

/**
 * Bind rebound global accelerators immediately, returning what the OS accepted.
 *
 * A REQUEST, not a fire-and-forget forward: whether a combination is available is
 * only knowable at register() time, so the Settings UI has to get an answer to
 * tell the user their key is taken. Absent shell resolves to `{}` — and there the
 * accelerators are not editable at all, because this store is their only copy and
 * `flattenConfig` does not post them to the gateway. The Settings pane gates the
 * editor on `hasShell` for exactly that reason.
 */
export async function applyShortcuts(
  accelerators: Record<string, string>,
): Promise<Record<string, boolean>> {
  const shell = (window as unknown as { mochi?: Record<string, unknown> }).mochi
  const fn = shell?.applyShortcuts
  if (typeof fn !== 'function') return {}
  try {
    return ((await (fn as (a: unknown) => Promise<unknown>)(accelerators)) ?? {}) as Record<
      string,
      boolean
    >
  } catch {
    return {}
  }
}

/**
 * The per-MACHINE prefs (`petInstance`, `shortcuts`) from the SHELL's own store.
 *
 * NOT over HTTP, and that is the point. Every Mochi window is loaded FROM the
 * gateway it shows, and this app's HTTP calls are same-origin — so a pet showing a
 * REMOTE read and wrote that remote's copy of a choice that belongs to this
 * computer, while the shell kept reading this machine's. That is what made the
 * instance switch a one-way door: the write landed where nothing reads it, the UI
 * read its own write back and looked successful, and no surface was left that
 * could move the pet home.
 *
 * `null` when there is no shell (a plain browser tab), which tells the caller to
 * fall back to the gateway's copy rather than to invent defaults.
 */
export async function machinePrefs(): Promise<{
  petInstance: string
  shortcuts: Record<string, string> | null
} | null> {
  const shell = (window as unknown as { mochi?: Record<string, unknown> }).mochi
  const fn = shell?.machinePrefs
  if (typeof fn !== 'function') return null
  try {
    const out = await (fn as () => Promise<unknown>)()
    return (out ?? null) as { petInstance: string; shortcuts: Record<string, string> | null } | null
  } catch {
    return null
  }
}

/**
 * Point the pet at an instance AND move it, in one shell call.
 *
 * One call, not "POST the setting then ask the shell to apply it": those were two
 * steps that could hit two different gateways, which is how a stored choice ended
 * up with nothing acting on it.
 */
export async function setPetInstance(instanceId: string): Promise<boolean> {
  const shell = (window as unknown as { mochi?: Record<string, unknown> }).mochi
  const fn = shell?.setPetInstance
  if (typeof fn !== 'function') return false
  try {
    const out = (await (fn as (id: string) => Promise<unknown>)(instanceId)) as
      | { ok?: boolean }
      | undefined
    return out?.ok === true
  } catch {
    return false
  }
}

/**
 * Core's instance list for THIS MACHINE's host gateway, as a full `InstancesView`.
 *
 * Carries the SAME four states the same-origin path produces. An earlier version
 * returned only `{known, instances}` and the caller rebuilt the view from it,
 * which silently erased `disabled` (multi-instance off) and `inactive` (needs
 * restart) — so every desktop user with the feature off saw just "This computer"
 * and none of the guidance that tells them what to do about it.
 *
 * `null` when there is no shell, so the caller can fall back to the same-origin
 * `/api/instances`. That fallback is correct in a browser tab (the tab IS the
 * host) and wrong only inside a pet already showing a remote — which is exactly
 * the case the shell path covers.
 */
export async function instancesList(): Promise<InstancesView | null> {
  const shell = (window as unknown as { mochi?: Record<string, unknown> }).mochi
  const fn = shell?.instancesList
  if (typeof fn !== 'function') return null
  try {
    const out = (await (fn as () => Promise<unknown>)()) as
      | { known?: boolean; state?: string; instances?: unknown[] }
      | undefined
    if (!out) return null
    const instances = (out.instances ?? []) as CoreInstance[]
    switch (out.state) {
      case 'disabled':
        return { state: 'disabled' }
      case 'inactive':
        return { state: 'inactive', instances }
      case 'ready':
        return { state: 'ready', instances }
      default:
        // Anything unrecognised is a non-answer, not an empty list — same
        // discipline as the same-origin path's catch.
        return { state: 'error' }
    }
  } catch {
    return null
  }
}

// ── Data + pet state/mood (same-origin HTTP/WS, shared with the panel) ───────
export {
  // Disabling the app is an HTTP action, not a shell ability — the pet closes
  // as a CONSEQUENCE of the app being disabled, via the reconcile loop.
  disableApp,
  getWatchlist,
  getPinnedFiles,
  markPinnedSeen,
  unpinFile,
  setWatchItemStatus,
  updateWatchItem,
  onWatchlistChanged,
  // Backend-owned pet state/mood (see the "Pet state / mood" note above).
  getPetState,
  getPetStateInfo,
  onStateChange,
  onMood,
  // Appearance packs added/removed (the Avatars window writing while the pet is
  // open); the per-setting change events are built on onAppearanceSettings below.
  onGalleryPacksChanged,
  onNotification,
} from '../panel/panelBridge'
// Aliased on import: the pet's exported onColorMapChanged reshapes this raw
// settings payload into the {packId, colorMap} the renderer expects.
import { onColorMapChanged as onColorMapSettings } from '../panel/panelBridge'
import type { CoreInstance, InstancesView } from '../panel/panelBridge'
import type { PackManifest } from '../src/shared/appearanceTypes'
import {
  BUILTIN_MOCHI_ID,
  resolveActivePackId,
} from '../builtinPacks'
import { resolvePackDetail } from '../packDetail'
import { subscribeAppEvent, reportStat } from '../panel/panelBridge'
import { PET_BOTTOM_MARGIN, PET_H, PET_W } from '../src/shared/constants'

