/**
 * `api` — the ONLY seam between the vendored original Mochi renderer
 * (`src/renderer/`, `src/shared/`) and this builtin.
 *
 * Every original renderer file reached the outside world through one handle:
 *
 *     const api = (window as any).mochi
 *
 * Each copied file replaces exactly that ONE line with `import { api } from
 * '../mochiApi'`. That keeps the components structurally identical to upstream
 * (so `git diff` against the pristine copy stays readable when KiroCrew's Mochi
 * lands here) while fixing three things the global handle got wrong:
 *
 *  1. It was `any`, so a missing method compiled fine and was then swallowed by
 *     the `?.` at the call site — the exact "renders correctly, does nothing"
 *     failure mode this migration kept producing. `api` is typed, so a wrong or
 *     absent method is a `tsc` error.
 *  2. The handle was captured at MODULE EVALUATION time, so any adapter that
 *     installed itself onto `window` had to run before the first component
 *     import. A plain import has no such ordering hazard.
 *  3. The Electron preload ALSO defines `window.mochi` (window management,
 *     click-through, menus). Merging on the global made the winner depend on
 *     load order; here the merge is explicit and greppable.
 *
 * Deliberately ABSENT (team sync decisions + architecture). The originals guard
 * every call with `?.`, so an absent method is a no-op rather than a throw — but
 * the corresponding UI is REMOVED rather than left dead:
 *   - self-updater: mochiCheckUpdate, mochiReinstall, devTestUpdate,
 *     devForceUpdateUI, devReinstall — KiroCrew owns updates.
 *   - backend switching / tunnel: tunnelConnect, tunnelStatus, onTunnelStatus,
 *     onBackendSwitching, onBackendSwitchError, onBackendResolved — the builtin
 *     is same-origin, so there is no backend to repoint.
 *   - soul: getSoul, setSoul — each avatar carries its own persona.
 *   - themes: broadcastTheme — Mochi follows the KiroCrew theme.
 *   - dictation: sendDictationTranscript, cancelDictation, onDictationState,
 *     onDictationStopRecording — push-to-talk is not ported.
 *   - onHide: DEAD UPSTREAM — the original's preload exposed it but nothing in
 *     its main process ever sent `pet:hide`.
 *   - `send` / `invoke`: a generic IPC relay is on the preload's never-expose
 *     list; exposing it would hand page content an arbitrary main-process call.
 */
// The built-in packs (art + the avatar -> pack mapping) live in one module so
// the pet, the gallery, and both change bridges resolve the active pack the
// same way. See builtinPacks.ts for why that mattered.
import {
  builtinPackMetas,
  isBuiltinPack,
} from '../builtinPacks'
import { resolvePackDetail } from '../packDetail'
import * as panel from '../panel/panelBridge'
import * as pet from '../pet/petBridge'
import { getSettings, getStats, updateSettings } from '../api'
import { PINNED_PANEL_WIDTH, WATCHLIST_PANEL_WIDTH } from './shared/constants'
import { buildWidgetPopoutHtml } from './shared/widgetPopout'
import type { MochiSettings, MochiSettingsPatch } from '../api'
import type { InstancesView } from '../panel/panelBridge'
import { DEFAULT_CONFIG } from './shared/config'
import type { AppConfig } from './shared/config'
import type { CatPreset } from './shared/catPresets'
import type { PackMeta, Result } from './shared/appearanceTypes'
import type { PetdexInstalled, PetdexPet } from '../petdexImport'

/**
 * Shell-only channels, provided by the Electron preload and absent in a plain
 * browser tab (window management, click-through, context menus).
 *
 * Declared EXPLICITLY rather than as an index signature: the whole point of this
 * module is that a name which exists nowhere becomes a compile error, and an
 * index signature would silently accept anything.
 */
interface ShellChannels {
  closeSettings?: () => void
  // Field names match the shell's own hit test (petWindow.js inRect reads w/h).
  setMenuHitbox?: (rect: { x: number; y: number; w: number; h: number } | null) => void
  menuOpened?: () => void
  menuClosed?: () => void
  setPanelWidth?: (width: number) => void
  /** Toggle every Mochi window (the hideAll accelerator's handler). */
  hideAll?: () => void
  avatarChosen?: () => void
  isElectron?: boolean
  platform?: string

  /**
   * Per-instance "is Mochi turned on there", keyed by instance id.
   *
   * Optional like every shell channel, and that is the point: in a browser tab
   * there is no shell to hold the remote tokens, so the switcher just omits the
   * badge instead of showing a control that cannot answer.
   */
  instancesEnabledMap?: () => Promise<Record<string, boolean>>
  /**
   * The per-MACHINE prefs from the shell's store, or null with no shell. Source of
   * truth for `petInstance` and the accelerators — see petBridge.machinePrefs.
   */
  machinePrefs?: () => Promise<{
    petInstance: string
    shortcuts: Record<string, string> | null
  } | null>
  /** Store the pointer AND move the pet, in one shell call. */
  setPetInstance?: (instanceId: string) => Promise<boolean>
  /**
   * Core's instance list for THIS MACHINE's host gateway as a full view; null with
   * no shell. Full view, not a boolean: the `disabled` / `inactive` states carry
   * the only guidance the pane gives, and rebuilding them from a flag loses them.
   */
  instancesList?: () => Promise<InstancesView | null>

  /**
   * Apply a just-saved `petInstance` now rather than on the shell's next reconcile
   * pass. Resolves after the windows have been rebuilt, so a caller can keep its
   * row busy until the pet has actually moved.
   */
  applyInstanceNow?: () => Promise<{ ok: boolean }>
}

function shellChannels(): ShellChannels {
  const injected = (window as unknown as { mochi?: ShellChannels }).mochi
  return injected ?? {}
}

/**
 * Upstream members this builtin does NOT implement.
 *
 * They are declared optional (and left undefined) so the vendored call sites —
 * every one of which is written `api?.name?.(...)` — keep compiling and become
 * no-ops. Declaring them here rather than deleting the call sites is what keeps
 * the vendored components diffable against upstream; the corresponding UI is
 * removed in the subtraction, so nothing dead is reachable.
 *
 * This list IS the inventory of what the port does not do. Adding a name here
 * must be a decision, not a shortcut.
 */
interface UnimplementedUpstream {
  /** Self-updater — KiroCrew owns updates. */
  mochiCheckUpdate?: () => Promise<string>
  mochiReinstall?: () => Promise<void>
  devTestUpdate?: () => Promise<void>
  devForceUpdateUI?: () => Promise<void>
  devReinstall?: () => Promise<void>
  quitApp?: () => void
  runDoctor?: () => Promise<{ name: string; ok: boolean; detail: string }[]>
  resetConfig?: () => Promise<void>
  /** Backend switching / tunnel — the builtin is same-origin. */
  onBackendSwitching?: (cb: (switching: boolean) => void) => () => void
  onBackendSwitchError?: (cb: (info: unknown) => void) => () => void
  onBackendResolved?: (cb: (info: unknown) => void) => () => void
  tunnelConnect?: (host: string) => Promise<unknown>
  tunnelStatus?: () => Promise<unknown>
  onTunnelStatus?: (cb: (info: unknown) => void) => () => void
  /** Themes — Mochi follows the KiroCrew theme. */
  broadcastTheme?: (id: string) => void
  /** Soul — each avatar carries its own persona. */
  getSoul?: () => Promise<unknown>
  setSoul?: (soul: string) => Promise<void>
  /**
   * Region capture for the MCP tool (fixed rect, no UI). The interactive path
   * IS implemented — see startCapture/onCaptureDone below.
   */
  sendCaptureRegion?: (region: unknown) => void
  /** In-panel navigation — Settings and Avatars are separate windows here. */
  onNavigate?: (cb: (route: string) => void) => () => void
  onSettingsCloseRequested?: (cb: () => void) => () => void
  /** A generic IPC relay is on the preload's never-expose list. */
  send?: never
}

/** Keys the original stored under `config.mochi` that this builtin does not own. */
const UNOWNED_MOCHI_KEYS = new Set([
  'autoCompactEnabled',
  'autoCompactThresholdPct',
  'soul',
  'theme',
])

/**
 * Appearance identity and colour state — owned by the Avatars/Gallery window.
 *
 * `nestConfig` spreads EVERY stored key into `config.mochi`, and the Settings
 * panel saves its whole config object. So a Settings save re-posted whichever
 * appearance was live when that panel LOADED, silently reverting a pack the user
 * applied in the meantime: "I clicked Apply and nothing happened", with no error,
 * because the write did land and was then overwritten. The pet then took its
 * built-in-cat branch, which never touches the pack machinery — hence no logs
 * either.
 *
 * These keys are dropped from `updateConfig` writes. Nothing is lost: the
 * Settings panel has no control for any of them, and each has a dedicated writer
 * (`gallerySetActive`, `gallerySetColorMap`, the preset calls) that posts the one
 * key it changed. A surface that cannot change a value must not write it.
 */
const GALLERY_OWNED_KEYS = new Set([
  'activeAppearance',
  'catPreset',
  'colorMaps',
  'customPresets',
])

/**
 * Keys the SHELL owns, dropped from `updateConfig` writes.
 *
 * `petInstance` is a property of this COMPUTER (one pet on one screen), not of any
 * gateway — see petBridge.machinePrefs for why storing it per-gateway made the
 * instance switch a one-way door. Posting it same-origin would write it onto
 * whichever gateway served the window, where nothing reads it, so the write is
 * dropped here and routed through IPC instead. `shortcuts` is handled the same way
 * in flattenConfig, for the same reason (one keyboard per machine).
 */
const SHELL_OWNED_MOCHI_KEYS = new Set(['petInstance'])

/**
 * Nest the builtin's flat settings into the tree the original renderer reads
 * (`config.mochi.*`, `config.shortcuts.*`, `config.window.*`).
 *
 * Only the paths the renderer actually reads are reconstructed; inventing the
 * rest would imply a persistence that does not exist.
 */
export function nestConfig(s: MochiSettings): NestedConfig {
  const flat = s as unknown as Record<string, unknown>
  return {
    mochi: {
      ...flat,
      // The original named the behaviour mode `activityMode`; the builtin's key
      // is `mode`. Both are published so either spelling reads correctly.
      activityMode: flat.mode,
      // Owned by KiroCrew core (Settings -> Chat), not by Mochi. Reported as the
      // core default so nothing reads `undefined` during the subtraction pass.
      autoCompactEnabled: true,
      autoCompactThresholdPct: 60,
      soul: '',
      theme: 'kirocrew',
    },
    shortcuts: (flat.shortcuts ?? {}) as Record<string, string>,
    window: { chatAlwaysOnTop: (flat.chatAlwaysOnTop as boolean | undefined) ?? true },
  }
}

export interface NestedConfig {
  mochi: Record<string, unknown>
  shortcuts: Record<string, string>
  window: { chatAlwaysOnTop: boolean }
}

/** Flatten an `updateConfig` partial back onto the builtin's flat store. */
export function flattenConfig(partial: Partial<NestedConfig>): MochiSettingsPatch {
  const out: Record<string, unknown> = {}
  if (partial.mochi !== undefined) {
    for (const [key, value] of Object.entries(partial.mochi)) {
      // Unowned keys are DROPPED rather than posted: the write route rejects
      // unknown keys, and one unowned key must not cost the user the rest of
      // the save.
      if (key === 'activityMode') out.mode = value
      else if (UNOWNED_MOCHI_KEYS.has(key) || GALLERY_OWNED_KEYS.has(key)) continue
      else if (SHELL_OWNED_MOCHI_KEYS.has(key)) continue
      else out[key] = value
    }
  }
  // NOT posted: the shell's store owns the accelerators, and `mochi-shortcuts:apply`
  // persists them there. Posting them here too would leave two copies that drift,
  // and the gateway copy is the one nothing reads. Because this drops them with no
  // fallback, the Settings pane must not offer the editor without a shell — it
  // gates on `api.hasShell`.
  if (partial.window !== undefined && 'chatAlwaysOnTop' in partial.window) {
    out.chatAlwaysOnTop = partial.window.chatAlwaysOnTop
  }
  return out as MochiSettingsPatch
}

async function readLocalImage(path: string): Promise<string | null> {
  // The original preload returned base64 and the caller wraps it in a
  // `data:image/png;base64,` URL, so a plain URL cannot be substituted. Core's
  // /api/file-raw is the transport because it already enforces the symlink and
  // sensitive-path checks the original had no equivalent for.
  try {
    const res = await fetch(panel.localFileUrl(path), { credentials: 'same-origin' })
    if (!res.ok) return null
    const bytes = new Uint8Array(await res.arrayBuffer())
    let binary = ''
    for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i])
    return btoa(binary)
  } catch {
    return null
  }
}

export interface McpServerRow {
  name: string
  core: boolean
  enabled: boolean
  agents: ('chat' | 'bg')[]
  autoApprove: string[]
  disabledTools: string[]
  chatPolicy: { autoApprove: string[]; disabledTools: string[] }
  bgPolicy: { autoApprove: string[]; disabledTools: string[] }
  toolCount?: number
}

/**
 * Servers already granted to Mochi's agents, so they are not "addable".
 *
 * Two sources: the app's own MCP server (declared by its manifest and written
 * into both agents) and the host-managed servers an agent references by name.
 * Kept as a predicate rather than a literal list so the `<app>:<server>` form
 * matches whatever the app is called.
 */
const HOST_MANAGED_MCP = new Set(['kirocrew-cron', 'kirocrew-core'])

function isBaselineMcpServer(name: string): boolean {
  return name.startsWith('mochi:') || HOST_MANAGED_MCP.has(name)
}

async function getMcpServers(): Promise<McpServerRow[]> {
  // The server INVENTORY comes from core (one MCP registry for the whole app);
  // the per-server assignment lives in Mochi's own settings. Both are needed:
  // without the merge every row would render default policy no matter what the
  // user configured.
  try {
    const [res, settings] = await Promise.all([
      // The core route is GET /api/mcp (see server.py's add_get for
      // api_mcp_servers) -- '/api/mcp/servers' does not exist and 404'd,
      // which rendered this section permanently empty.
      fetch('/api/mcp', { credentials: 'same-origin' }),
      getSettings().catch(() => undefined),
    ])
    if (!res.ok) return []
    // Core returns a BARE ARRAY of server dicts (handlers/mcp.py builds
    // `result: list[dict]`), not { servers: [...] } — reading a wrapper key
    // here rendered the MCP section permanently empty.
    const body = (await res.json()) as unknown
    const inventory = (Array.isArray(body)
      ? body
      : Array.isArray((body as { servers?: unknown[] })?.servers)
        ? (body as { servers: unknown[] }).servers
        : []) as Record<string, unknown>[]
    const configured = new Map<string, Partial<McpServerRow>>()
    for (const entry of settings?.extraMcpServers ?? []) {
      if (typeof entry === 'string') configured.set(entry, { name: entry })
      else if (typeof entry?.name === 'string') configured.set(entry.name, entry)
    }
    return inventory.map((server) => {
      const name = String(server.name ?? '')
      const own = configured.get(name)
      return {
        name,
        // `core` means "already in this agent's baseline grant", which the panel
        // renders as always-on instead of offering it under "Available to add".
        // Hard-coding false listed the app's OWN server and the host-managed
        // ones as addable even though they are already granted.
        core: isBaselineMcpServer(name),
        enabled: own !== undefined,
        agents: own?.agents ?? ['chat'],
        autoApprove: own?.autoApprove ?? [],
        disabledTools: own?.disabledTools ?? [],
        chatPolicy: { autoApprove: [], disabledTools: [] },
        bgPolicy: { autoApprove: [], disabledTools: [] },
        toolCount: Array.isArray(server.tools)
          ? server.tools.length
          : typeof server.tool_count === 'number' ? server.tool_count : undefined,
      }
    })
  } catch {
    return []
  }
}

async function discoverMcpTools(
  name: string,
): Promise<{
  tools: { name: string; description?: string }[]
  fromCache: boolean
  errorCode?: string
} | null> {
  // Mochi's OWN route, not core's. Core has GET /api/mcp (whole inventory) and
  // PUT/DELETE on /api/mcp/servers/{name} — no per-server read, so this used to
  // resolve the path, miss on method, take a 405, and return null. Same class of
  // bug as the inventory fetch above.
  //
  // POST because the route spawns a process: CSRF exempts GET, so a discover
  // reachable by GET could be triggered by a cross-site navigation.
  try {
    const res = await fetch(`/api/apps/mochi/mcp-tools/${encodeURIComponent(name)}`, {
      method: 'POST',
      credentials: 'same-origin',
    })
    const body = (await res.json().catch(() => ({}))) as {
      tools?: { name: string; description?: string }[]
      cached?: boolean
      code?: string
      status?: string
    }
    // Report a CODE, never the server's prose: the panel renders in 10 locales,
    // so an English `error` string (or a raw "HTTP 405") is untranslatable at the
    // point of display. The backend already emits a machine-readable `code` for
    // exactly this; fall back to a synthetic one so the panel always has a key.
    //
    // The fallback is one FIXED token, not `http_${status}`: every code here has
    // to be a member of a closed set the panel can map to a catalog key, and an
    // arbitrary status number is unmappable by construction -- it would land on
    // the generic line anyway. Named failures (403 app_disabled, 409 disabled)
    // already arrive as `body.code`, so this only fires for genuinely unexpected
    // HTTP failures, which share one message.
    if (!res.ok) {
      return { tools: [], fromCache: false, errorCode: body.code || 'http_error' }
    }
    // A probe that reached the server but failed (missing binary, handshake
    // error) answers 200 with status != "ok" and no tools. Without this it read
    // as "this server simply has no tools" -- the same silent-success shape this
    // fix set out to remove, just one layer down.
    const status = body.status
    if (status && status !== 'ok') {
      return { tools: body.tools ?? [], fromCache: false, errorCode: 'probe_failed' }
    }
    return { tools: body.tools ?? [], fromCache: body.cached === true }
  } catch {
    return { tools: [], fromCache: false, errorCode: 'network' }
  }
}

async function updateSttConfig(patch: Record<string, unknown>): Promise<void> {
  try {
    await fetch('/api/config/stt', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
  } catch {
    // Voice is optional; a failed write must not take the settings window down.
  }
}

/**
 * Remaining pack-authoring paths. Both take an OS PATH from a native dialog,
 * which a page cannot read — export and bundle import are implemented above.
 *
 * They report an explicit failure instead of being absent: the vendored Avatars
 * window renders `result.error`, so the user sees why nothing happened rather
 * than clicking a button that silently does nothing.
 */
async function notSupported(_arg?: unknown): Promise<Result<never>> {
  return { ok: false, error: 'Not available in this build' }
}

/**
 * Pack detail, in the shape the vendored renderer expects: the builtin's route
 * returns the manifest as stored (state/mood values are FILENAMES), while the
 * original's IPC handed back a flattened `animations` map. Flattening is the
 * whole point of the seam. Defined with the built-in registry that also
 * produces it; re-exported so the seam's public surface is unchanged.
 */
export type { InlineAnimation, PackDetail } from '../builtinPacks'
import type { PackDetail } from '../builtinPacks'


/**
 * Pick a sprite sheet from disk.
 *
 * Upstream opened a native file dialog from the main process; a page cannot, so
 * this uses a transient `<input type="file">`. Two things it must get right that
 * the previous stub could not: the promise has to resolve on CANCEL (the
 * importer awaits it, so a dropped promise would wedge the button), and the
 * data URI has to carry the file's REAL mime — petdex ships WebP, and hard-coding
 * `image/png` left the decode to browser sniffing.
 */
async function importSpriteFile(): Promise<Result<{ content: string; mime: string }> | null> {
  return new Promise((resolve) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/png,image/webp,image/gif,image/jpeg'
    input.style.display = 'none'
    let settled = false
    const finish = (value: Result<{ content: string; mime: string }> | null) => {
      if (settled) return
      settled = true
      input.remove()
      resolve(value)
    }
    // `cancel` is not universally implemented; a focus-return fallback would
    // race a slow dialog, so an unsupported cancel simply leaves the promise to
    // the change event. The input is removed either way.
    input.addEventListener('cancel', () => finish(null))
    input.addEventListener('change', () => {
      const file = input.files?.[0]
      if (!file) return finish(null)
      const reader = new FileReader()
      reader.onerror = () => finish({ ok: false, error: 'Could not read that file' })
      reader.onload = () => {
        const uri = String(reader.result ?? '')
        const comma = uri.indexOf(',')
        if (comma < 0) return finish({ ok: false, error: 'Could not read that file' })
        const mime = /^data:([^;,]+)/.exec(uri)?.[1] || file.type || 'image/png'
        finish({ ok: true, value: { content: uri.slice(comma + 1), mime } })
      }
      reader.readAsDataURL(file)
    })
    document.body.appendChild(input)
    input.click()
  })
}

// ── petdex.dev import ────────────────────────────────────────────────────
//
// The backend does the obtaining (see petdex_import.py): the remote branch is
// an outbound request to a third party and belongs behind the gateway's
// allow-list and byte caps, not in page script. The row -> slot mapping is NOT
// decided here — petdex files carry no mapping, so it is a user choice made in
// the importer.

/** Pets the petdex CLI already installed locally. Never throws: drives a list. */
async function petdexListInstalled(): Promise<PetdexInstalled[]> {
  try {
    const res = await fetch('/api/apps/mochi/petdex/installed', { credentials: 'same-origin' })
    if (!res.ok) return []
    const body = (await res.json()) as { pets?: PetdexInstalled[] }
    return Array.isArray(body?.pets) ? body.pets : []
  } catch {
    return []
  }
}

/** Obtain one pet. `source: 'remote'` reaches petdex.dev; 'local' reads disk. */
async function petdexImport(
  slug: string,
  source: 'local' | 'remote',
): Promise<Result<PetdexPet>> {
  try {
    const res = await fetch('/api/apps/mochi/petdex/import', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug, source }),
    })
    const body = (await res.json().catch(() => null)) as
      | (PetdexPet & { ok?: boolean; error?: string })
      | null
    if (!res.ok || !body || body.ok !== true) {
      return { ok: false, error: body?.error || `Import failed (HTTP ${res.status})` }
    }
    return { ok: true, value: body }
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : 'Import failed' }
  }
}

/**
 * Export a pack as `.mochipack.zip`.
 *
 * Upstream showed a native save dialog and wrote the file itself. A page cannot,
 * so this triggers an ordinary browser download — the format is identical, so a
 * bundle exported here imports upstream and vice versa.
 */
async function galleryExport(packId: string): Promise<Result<null>> {
  try {
    const res = await fetch(`/api/apps/mochi/packs/${encodeURIComponent(packId)}/export`, {
      credentials: 'same-origin',
    })
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { error?: string }
      return { ok: false, error: body.error ?? `Export failed (${res.status})` }
    }
    const url = URL.createObjectURL(await res.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = `${packId}.mochipack.zip`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
    return { ok: true, value: null }
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : 'Export failed' }
  }
}

/** Prompt for a file and resolve its bytes, or null if the user cancelled. */
function pickFile(accept: string): Promise<File | null> {
  return new Promise((resolve) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = accept
    // `cancel` is not universally emitted, so a cancelled dialog simply never
    // resolves — acceptable here because the caller has no timeout semantics
    // either, and the promise is dropped with the page.
    input.onchange = () => resolve(input.files?.[0] ?? null)
    input.click()
  })
}

async function galleryImportBundle(): Promise<Result<PackMeta> | null> {
  const file = await pickFile('.zip,.mochipack.zip,application/zip')
  if (file === null) return null // cancelled — upstream returns nothing too
  try {
    const res = await fetch('/api/apps/mochi/packs/import', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/zip' },
      body: file,
    })
    const body = (await res.json().catch(() => ({}))) as { error?: string; meta?: PackMeta }
    if (!res.ok || body.meta === undefined) {
      return { ok: false, error: body.error ?? `Import failed (${res.status})` }
    }
    return { ok: true, value: body.meta }
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : 'Import failed' }
  }
}

/** Built-ins first, then the user's packs — upstream's order. */
async function galleryListPacks(): Promise<PackMeta[]> {
  const custom = await panel.galleryListPacks()
  const builtins = builtinPackMetas() as unknown as PackMeta[]
  return [...builtins, ...custom.filter((p) => !isBuiltinPack(p.id))]
}

async function galleryGetPackDetail(packId: string): Promise<PackDetail | null> {
  // Shared with the pet's live appearance-switch path — see packDetail.ts for
  // why having a second, manifest-shaped builder there was the bug.
  return resolvePackDetail(packId)
}

/** `gallerySetActive` / `galleryDeletePack` return void/boolean; the vendored UI
 *  reads a Result so it can surface the reason. */
async function gallerySetActive(packId: string): Promise<Result<null>> {
  try {
    // One key for every pack, built-in or imported: the id IS the choice, and
    // the persona and default name follow it on the backend.
    await panel.gallerySetActive(packId)
    return { ok: true, value: null }
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : 'Failed to apply pack' }
  }
}

async function galleryDelete(packId: string): Promise<Result<null>> {
  const removed = await panel.galleryDeletePack(packId)
  return removed ? { ok: true, value: null } : { ok: false, error: 'Delete failed' }
}

/** Read one file out of a pack as base64 (the editor slices it on a canvas). */
async function galleryReadPackFile(packId: string, filename: string): Promise<string | null> {
  try {
    const res = await fetch(panel.galleryPackFileUrl(packId, filename), {
      credentials: 'same-origin',
    })
    if (!res.ok) return null
    const bytes = new Uint8Array(await res.arrayBuffer())
    let binary = ''
    for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i])
    return btoa(binary)
  } catch {
    return null
  }
}

/**
 * Open a generated widget's HTML in a browser tab.
 *
 * NOT `openExternal`: that takes a URL, and handing it a whole HTML document
 * would try to navigate to the markup. A blob URL is the transport instead.
 *
 * Security: the widget is UNTRUSTED LLM markup. A blob: URL inherits the
 * dashboard's origin, so opening the raw srcdoc as a TOP-LEVEL document would
 * let it read/clear the dashboard's localStorage/cookies (e.g. a widget that
 * calls `localStorage.clear()`). So we don't open the widget directly — we open
 * a tiny host page whose only content is the same `sandbox="allow-scripts"`
 * (null-origin) iframe WidgetFrame uses in-panel, and put the widget inside it.
 * The widget therefore stays origin-isolated in the popped-out tab too.
 */
function openWidgetExternal(srcdoc: string, title?: string): void {
  const blob = new Blob([buildWidgetPopoutHtml(srcdoc, title)], { type: 'text/html' })
  const url = URL.createObjectURL(blob)
  window.open(url, '_blank', 'noopener')
  // Revoked on a delay so the new tab has time to fetch it.
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

/**
 * Interactive screen capture.
 *
 * The original shelled out to macOS's `/usr/sbin/screencapture -i`, which is
 * Darwin-only — this build ships on Windows too. Capture therefore runs on
 * KiroCrew's OWN mechanism (`captureScreen` + `SnipOverlay`, with
 * `session.setDisplayMediaRequestHandler` already registered in the shell and
 * the macOS Screen Recording permission already gated + explained there). One
 * capture path, one permission path, all three platforms.
 *
 * The flow is split because the seam is not a React tree: `startCapture()` only
 * RAISES the request; `MochiSnipHost` (mounted by the panel entry) performs it
 * and calls `deliverCapture` with the PNG base64 that the vendored ChatPanel
 * expects from `onCaptureDone`.
 */
const START_CAPTURE_EVENT = 'mochi:start-capture'
const captureListeners = new Set<(base64: string) => void>()

function startCapture(): void {
  window.dispatchEvent(new CustomEvent(START_CAPTURE_EVENT))
}

function onCaptureDone(cb: (base64: string) => void): () => void {
  captureListeners.add(cb)
  return () => {
    captureListeners.delete(cb)
  }
}

/** Called by MochiSnipHost once the user has selected a region. */
export function deliverCapture(base64: string): void {
  for (const cb of captureListeners) cb(base64)
}

/** Subscribe to capture requests (used by MochiSnipHost, not by vendored code). */
export function onCaptureRequested(cb: () => void): () => void {
  const handler = () => cb()
  window.addEventListener(START_CAPTURE_EVENT, handler)
  return () => window.removeEventListener(START_CAPTURE_EVENT, handler)
}

/**
 * Reset Mochi: forget everything and go back to the orange cat.
 *
 * Two steps, in this order and for a reason. The chat SLOT belongs to KiroCrew
 * core, so it is cleared through core's own endpoint rather than by the app
 * reaching into another subsystem's storage; the app route then clears Mochi's
 * own state and rewrites its settings to defaults.
 *
 * The history goes FIRST: if it fails the user still has their settings, which is
 * the recoverable half. It is a real DELETE, not a new session — archiving the
 * slot left the conversation (and its preview) in KiroCrew's session list, so a
 * reset looked like it had not happened until the next message forced a reload.
 *
 * User-imported appearance packs are deliberately kept —
 * they are the user's own art, and upstream's dialog only ever promised history,
 * activity logs, screenshots and learned preferences.
 */
async function resetMochi(): Promise<void> {
  await panel.deleteHistory()
  const res = await fetch('/api/apps/mochi/reset', {
    method: 'POST',
    credentials: 'same-origin',
  })
  if (!res.ok) throw new Error(`reset failed: ${res.status}`)
}

/**
 * Save a pack from per-slot content (the pack editor's Save).
 *
 * `gallerySaveSpritePack` only covers the sprite-sheet importer, so without this
 * the Avatars window's "Create new" / "Edit" buttons had nothing behind them.
 */
async function gallerySavePack(pack: unknown): Promise<Result<PackMeta>> {
  try {
    const res = await fetch('/api/apps/mochi/packs/content', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pack),
    })
    const body = (await res.json().catch(() => ({}))) as { error?: string; meta?: PackMeta }
    if (!res.ok || body.meta === undefined) {
      return { ok: false, error: body.error ?? `Save failed (${res.status})` }
    }
    return { ok: true, value: body.meta }
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : 'Save failed' }
  }
}

/** Persist one pack's colour map without clobbering the others. */
async function gallerySetColorMap(packId: string, map: unknown): Promise<void> {
  const current = await getSettings().catch(() => undefined)
  await updateSettings({ colorMaps: { ...(current?.colorMaps ?? {}), [packId]: map } })
}

async function presetsLoadCustom(): Promise<CatPreset[]> {
  const settings = await getSettings().catch(() => undefined)
  return (settings?.customPresets ?? []) as CatPreset[]
}

async function presetsSaveCustom(presets: CatPreset[]): Promise<void> {
  await updateSettings({ customPresets: presets })
}

/**
 * Base chat-panel width, matching the Electron panel window's own default.
 *
 * The side-panel toggles must be computed from a BASE plus the visible rails,
 * never from the window's current width: reading the current width and adding to
 * it accumulates, which is how the original's main process ended up with a
 * `baseWidth` state machine whose comment reads "prevents width corruption when
 * toggling panels in any order".
 */
const BASE_PANEL_WIDTH = 320

const railsVisible = { watch: false, pinned: false }

function applyPanelWidth(): void {
  const width =
    BASE_PANEL_WIDTH +
    (railsVisible.watch ? WATCHLIST_PANEL_WIDTH : 0) +
    (railsVisible.pinned ? PINNED_PANEL_WIDTH : 0)
  shellChannels().setPanelWidth?.(width)
}

async function toggleWatchPanel(visible: boolean): Promise<void> {
  railsVisible.watch = visible
  applyPanelWidth()
}

async function togglePinnedPanel(visible: boolean): Promise<void> {
  railsVisible.pinned = visible
  applyPanelWidth()
}

/**
 * Pet context-menu dispatch.
 *
 * The original relayed a menu id to the main process, which owned the menu. Here
 * the menu lives in the renderer and the actions are already individual bridge
 * calls, so this maps ids onto them. An unknown id is ignored rather than
 * thrown: a menu entry that outlives its handler should be inert, not fatal.
 */
function contextMenuAction(action: string): void {
  const handlers: Record<string, () => void> = {
    chat: pet.openChat,
    openChat: pet.openChat,
    avatars: pet.openAvatars,
    gallery: pet.openAvatars,
    settings: pet.openSettings,
    memories: pet.openMemories,
    dashboard: panel.openDashboard,
    // Hide, NOT disable: disabling tears the app down through the app
    // manager (seconds of work) and is a different user intent entirely.
    hide: () => shellChannels().hideAll?.(),
    disable: () => void panel.disableApp(),
  }
  handlers[action]?.()
}

/**
 * Notification quiet mode (DND).
 *
 * Read returns the quiet expiry (ms epoch, 0 = not quiet) from the pet-state
 * pull; write enters (`minutes > 0`) or leaves (`0`) quiet mode. Both degrade
 * to "not quiet" on failure — a menu that cannot reach the gateway should show
 * the normal item, not throw.
 */
async function getQuietUntil(): Promise<number> {
  try {
    const res = await fetch('/api/apps/mochi/pet-state', { credentials: 'same-origin' })
    if (!res.ok) return 0
    const body = (await res.json()) as { silentUntil?: unknown }
    return typeof body.silentUntil === 'number' ? body.silentUntil : 0
  } catch {
    return 0
  }
}

async function setQuiet(minutes: number): Promise<number> {
  try {
    const res = await fetch('/api/apps/mochi/quiet', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ minutes }),
    })
    if (!res.ok) return 0
    const body = (await res.json()) as { silentUntil?: unknown }
    return typeof body.silentUntil === 'number' ? body.silentUntil : 0
  } catch {
    return 0
  }
}

/**
 * The composed handle. Spread order is deliberate: shell channels first, then
 * the web transports, so a name implemented on BOTH resolves to the web
 * transport (which works in a browser tab as well as in the shell).
 */
const impl = {
  ...shellChannels(),
  ...panel,
  ...pet,

  getStats,
  getWatchlist: panel.getWatchlistItems,
  // Cast because nestConfig reconstructs only the paths the renderer reads;
  // claiming a complete AppConfig would be a lie, but the vendored call sites
  // are typed against it.
  getConfig: async (): Promise<AppConfig> => {
    const nested = nestConfig(await getSettings())
    // Overlay the per-MACHINE prefs from the SHELL, which is their source of
    // truth. Without this the Settings window shows the copy belonging to
    // whichever gateway served it — and on a pet that is already showing a
    // remote, that is the wrong machine's answer.
    //
    // A null result means there is no shell (a plain browser tab), where the
    // gateway's own copy is the best available answer — so the pre-shell
    // behaviour is preserved rather than replaced by invented defaults.
    const prefs = await pet.machinePrefs()
    if (prefs) {
      if (typeof prefs.petInstance === 'string') nested.mochi.petInstance = prefs.petInstance
      // MERGED over the full defaults, never assigned wholesale. Neither source is
      // guaranteed complete: the gateway's flat store may hold no accelerators at
      // all, and the shell's store deliberately OMITS an action it has no usable
      // value for (see mergeAcceptedShortcuts) so shortcuts.js can fall back to
      // its own defaults. The renderer has no such fallback — `displayAccelerator`
      // splits the string — so a missing key throws while rendering the Shortcuts
      // pane and React tears down the whole panel.
      if (prefs.shortcuts) {
        nested.shortcuts = {
          ...DEFAULT_CONFIG.shortcuts,
          ...nested.shortcuts,
          ...prefs.shortcuts,
        }
      }
    }
    return nested as unknown as AppConfig
  },
  /**
   * True only inside an Electron Mochi window, where shell IPC exists.
   *
   * A BOOLEAN, and deliberately not a method-presence test: the shell-backed
   * members below (`machinePrefs`, `setPetInstance`, `instancesList`) are
   * assigned unconditionally from petBridge, so they are DEFINED in a plain
   * browser tab too and merely resolve null/false there. A `!api.setPetInstance`
   * style guard therefore never fires and silently renders a dead control.
   */
  hasShell: pet.hasShell,
  machinePrefs: pet.machinePrefs,
  setPetInstance: pet.setPetInstance,
  instancesList: pet.instancesList,
  updateConfig: async (partial: Partial<NestedConfig>): Promise<void> => {
    const flat = flattenConfig(partial)
    if (Object.keys(flat).length === 0) return
    await updateSettings(flat)
  },

  galleryOpen: pet.openAvatars,
  galleryDelete,
  gallerySetActive,
  gallerySetColorMap,
  presetsLoadCustom,
  presetsSaveCustom,

  getMcpServers,
  getQuietUntil,
  setQuiet,
  discoverMcpTools,
  updateSttConfig,
  readLocalImage,
  openWidgetExternal,
  startCapture,
  onCaptureDone,
  galleryGetPackDetail,
  galleryListPacks,
  galleryReadPackFile,
  galleryExport,
  galleryImportBundle,
  galleryImportFile: notSupported,
  importSpriteFile,
  petdexListInstalled,
  petdexImport,
  gallerySavePack,

  resetMochi,
  toggleWatchPanel,
  togglePinnedPanel,
  contextMenuAction,
}

export const api: typeof impl & UnimplementedUpstream = impl
