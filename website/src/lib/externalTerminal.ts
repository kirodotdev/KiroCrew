/**
 * "Open in external terminal": the renderer half of the desktop shell's
 * external-terminal launch.
 *
 * The launch is two steps and the renderer holds neither the binary nor the
 * argv at any point:
 *   1. `POST /api/terminal/external` asks the gateway to approve opening the
 *      picked app in `projectDir`. The gateway is the gate (owner, direct-local
 *      request, integrated terminal enabled, an app picked) and answers an
 *      opaque single-use grant.
 *   2. The grant goes to the desktop shell's `externalTerminalAPI.open`, whose
 *      main process redeems it at the gateway and only then starts the app.
 *
 * Every refusal resolves `{ ok: false }` rather than throwing, so the caller
 * can fall back to the integrated terminal.
 */

/** One allowlisted terminal app installed on this machine. */
export interface ExternalTerminalApp {
  id: string
  label: string
}

/**
 * The note a refused or failed launch leaves while the integrated terminal
 * opens in its place. `offerSettings` is true for the notes that send the user
 * to Settings → Display → Terminal, which the host then links to.
 */
export interface ExternalTerminalFallbackNote {
  message: string
  offerSettings: boolean
}

interface ExternalTerminalBridge {
  detect: () => Promise<{ apps?: unknown; capable?: unknown }>
  open: (grant: string) => Promise<{ ok?: boolean; app?: string; error?: string }>
}

function bridge(): ExternalTerminalBridge | undefined {
  const api = (window as { externalTerminalAPI?: Partial<ExternalTerminalBridge> }).externalTerminalAPI
  return typeof api?.detect === 'function' && typeof api?.open === 'function'
    ? (api as ExternalTerminalBridge)
    : undefined
}

/**
 * Whether this window can launch an external terminal at all: true only in
 * the desktop shell, whose preload exposes the bridge. A plain browser tab and
 * the PWA have no bridge, so the row and the picker stay hidden there. Read
 * lazily so a test can stub `window.externalTerminalAPI` per case.
 */
export function canOpenExternalTerminal(): boolean {
  return bridge() !== undefined
}

/**
 * What the desktop shell found: whether this window may launch an app on this
 * computer at all (`capable`), and if so the allowlisted terminal apps
 * installed here. A window the shell refuses, such as one reaching a remote
 * crew through an SSH tunnel, answers `capable: false` with no apps, and the
 * row and the picker stay hidden there.
 */
export interface ExternalTerminalDetection {
  capable: boolean
  apps: ExternalTerminalApp[]
}

/**
 * The allowlisted terminal apps installed on this machine, as the desktop
 * shell detects them. Not capable, with no apps, when there is no bridge (a
 * browser tab) or the shell refuses this window. Rejects when the shell
 * answers malformed or the check itself fails, so a failed check is reported
 * as a failure rather than read as "nothing installed".
 */
export async function detectExternalTerminals(): Promise<ExternalTerminalDetection> {
  const api = bridge()
  if (!api) return { capable: false, apps: [] }
  const answer = await api.detect()
  if (answer?.capable === false) return { capable: false, apps: [] }
  if (!Array.isArray(answer?.apps)) throw new Error('external terminal detection answered without an app list')
  return {
    capable: true,
    apps: answer.apps.filter(
      (a): a is ExternalTerminalApp =>
        !!a && typeof (a as ExternalTerminalApp).id === 'string' && typeof (a as ExternalTerminalApp).label === 'string',
    ),
  }
}

/**
 * Why an external launch fell back, one state per sentence the user can act
 * on: the gateway found no app picked (`unpicked`), the desktop shell found the
 * picked app not installed (`not_installed`), this window may not launch apps
 * on this computer (`not_local`), or the app did not start (`failed`).
 * `error` is the code {@link openInExternalTerminal} resolved with.
 */
export function fallbackReason(error: string | undefined): 'unpicked' | 'not_installed' | 'not_local' | 'failed' {
  if (error === 'external_terminal_not_configured') return 'unpicked'
  if (error === 'no-app') return 'not_installed'
  if (
    error === 'external_terminal_not_local'
    || error === 'owner_only'
    || error === 'http_401'
    || error === 'unavailable'
  ) return 'not_local'
  return 'failed'
}

/**
 * Open the picked terminal app in `projectDir`. Resolves `{ ok: true }` once
 * the app has started, else `{ ok: false, error }` where `error` is the
 * gateway's refusal code, the shell's launch error, or `'unavailable'`.
 */
export async function openInExternalTerminal(projectDir: string): Promise<{ ok: boolean; error?: string }> {
  const api = bridge()
  if (!api) return { ok: false, error: 'unavailable' }
  let grant: unknown
  try {
    const res = await fetch('/api/terminal/external', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cwd: projectDir }),
    })
    const body = (await res.json().catch(() => null)) as { grant?: unknown; code?: unknown } | null
    if (!res.ok) return { ok: false, error: typeof body?.code === 'string' ? body.code : `http_${res.status}` }
    grant = body?.grant
  } catch {
    return { ok: false, error: 'request_failed' }
  }
  if (typeof grant !== 'string' || !grant) return { ok: false, error: 'no_grant' }
  try {
    const result = await api.open(grant)
    return result?.ok === true ? { ok: true } : { ok: false, error: result?.error || 'launch_failed' }
  } catch {
    return { ok: false, error: 'launch_failed' }
  }
}
