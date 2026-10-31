/**
 * Quick asks: the few things a browsing agent needs a person for (sign in, a
 * field only they know, a one-time code, an "I'm not a robot" box, approving
 * one press of a button), offered as a fixed set of typed asks the person
 * clicks instead of free chat.
 *
 * The wire contract (the gateway's `browser_live` event carries `ask`, and the
 * answer goes to `POST /api/browser/ask/answer`):
 *   - `origin` is the page's address as the BROWSER reports it, never text the
 *     agent or the page wrote. It is shown in full on every ask.
 *   - `kind` is one of five fixed values; the TITLE for each is ours (i18n).
 *     The agent supplies only `reason`, shown as plain text, at most two lines.
 *   - A typed value leaves this page in exactly one place: the answer POST. The
 *     gateway writes it into the bound element; the agent never reads it back.
 *   - A human check is never solved by the agent; the ask hands the person a
 *     takeover scoped to that widget.
 */
export type QuickAskKind = 'credentials' | 'text' | 'otp' | 'human_check' | 'confirm'
/** `region`: the box a sign-in takeover zooms on (credentials with no field bound). */
export type QuickAskRole = 'username' | 'password' | 'text' | 'otp' | 'widget' | 'action' | 'region'
export type QuickAskInput = 'text' | 'email' | 'tel' | 'password' | 'otp'
export type QuickAskLabel = 'name' | 'email' | 'username' | 'phone' | 'other'

export interface QuickAskRect { x: number; y: number; w: number; h: number }

/** One page element the ask writes to or points at; `rect` is in frame pixels. */
export interface QuickAskField {
  role: QuickAskRole
  input?: QuickAskInput
  label?: QuickAskLabel
  rect: QuickAskRect
}

export interface QuickAsk {
  id: string
  kind: QuickAskKind
  /** Origin of the frame the ask writes into, from the browser, e.g. `https://idmsa.apple.com`. */
  origin: string
  /** The tab's own origin when that frame is an iframe from another origin. */
  top_origin?: string
  /** Agent-authored; plain text on display. */
  reason: string
  fields: QuickAskField[]
  /** The frame size `fields[].rect` is measured in. */
  frame: { w: number; h: number }
  /** otp: how many digits. */
  digits?: number
  /** confirm: what will happen, in the AGENT's words (shown labelled as such). */
  summary?: string
  /** confirm: the bound button's own text, read from the DOM, not from the agent. */
  button?: string
  /**
   * credentials with no field bound: the gateway walks the sign-in itself,
   * screen by screen, on `origin` only. `username` says whether the sheet asks
   * for one (`none`: the page showed only a password box).
   */
  steps?: { username: QuickAskUsername }
  /** Epoch ms after which the gateway answers `stale`. */
  expires_at: number
  phase: 'waiting' | 'taken_over'
}

export type QuickAskUsername = 'required' | 'optional' | 'none'

export type QuickAskAction = 'fill' | 'decline' | 'take_over' | 'done' | 'confirm' | 'cancel'
export interface QuickAskValues { username?: string; password?: string; text?: string; otp?: string }
export type QuickAskError = 'origin_changed' | 'stale' | 'not_owner' | 'too_large' | 'bad_action' | 'no_field'
export interface QuickAskAnswer { ok: boolean; outcome?: string; error?: QuickAskError | string }

const KINDS: ReadonlySet<string> = new Set(['credentials', 'text', 'otp', 'human_check', 'confirm'])
const ROLES: ReadonlySet<string> = new Set(['username', 'password', 'text', 'otp', 'widget', 'action', 'region'])
const INPUTS: ReadonlySet<string> = new Set(['text', 'email', 'tel', 'password', 'otp'])
const LABELS: ReadonlySet<string> = new Set(['name', 'email', 'username', 'phone', 'other'])

const ORIGIN_RE = /^https?:\/\/[^\s/]+$/i

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function readRect(v: unknown): QuickAskRect | null {
  if (!v || typeof v !== 'object') return null
  const { x, y, w, h } = v as Record<string, unknown>
  return finite(x) && finite(y) && finite(w) && finite(h) && w >= 0 && h >= 0 ? { x, y, w, h } : null
}

/**
 * The event's `ask`, field by field; null for anything the card must not act
 * on. Only an http(s) origin is accepted: it is the one line a person checks.
 */
export function readQuickAsk(raw: unknown): QuickAsk | null {
  if (!raw || typeof raw !== 'object') return null
  const d = raw as Record<string, unknown>
  if (typeof d.id !== 'string' || !d.id) return null
  if (typeof d.kind !== 'string' || !KINDS.has(d.kind)) return null
  if (typeof d.origin !== 'string' || !ORIGIN_RE.test(d.origin)) return null
  const frame = d.frame && typeof d.frame === 'object' ? d.frame as Record<string, unknown> : null
  if (!frame || !finite(frame.w) || !finite(frame.h) || frame.w <= 0 || frame.h <= 0) return null
  const fields: QuickAskField[] = []
  for (const f of Array.isArray(d.fields) ? d.fields : []) {
    if (!f || typeof f !== 'object') continue
    const r = f as Record<string, unknown>
    const rect = readRect(r.rect)
    if (typeof r.role !== 'string' || !ROLES.has(r.role) || !rect) continue
    fields.push({
      role: r.role as QuickAskRole,
      ...(typeof r.input === 'string' && INPUTS.has(r.input) ? { input: r.input as QuickAskInput } : {}),
      ...(typeof r.label === 'string' && LABELS.has(r.label) ? { label: r.label as QuickAskLabel } : {}),
      rect,
    })
  }
  const digits = finite(d.digits) && Number.isInteger(d.digits) && d.digits >= 4 && d.digits <= 10 ? d.digits : undefined
  const stepsRaw = d.kind === 'credentials' && d.steps && typeof d.steps === 'object' ? (d.steps as Record<string, unknown>).username : undefined
  const steps = stepsRaw === 'required' || stepsRaw === 'optional' || stepsRaw === 'none' ? { username: stepsRaw as QuickAskUsername } : undefined
  return {
    id: d.id,
    kind: d.kind as QuickAskKind,
    origin: d.origin,
    ...(typeof d.top_origin === 'string' && ORIGIN_RE.test(d.top_origin) && d.top_origin !== d.origin ? { top_origin: d.top_origin } : {}),
    reason: typeof d.reason === 'string' ? d.reason : '',
    fields,
    frame: { w: frame.w, h: frame.h },
    ...(digits ? { digits } : {}),
    ...(typeof d.summary === 'string' ? { summary: d.summary } : {}),
    ...(typeof d.button === 'string' ? { button: d.button } : {}),
    ...(steps ? { steps } : {}),
    expires_at: finite(d.expires_at) ? d.expires_at : 0,
    phase: d.phase === 'taken_over' ? 'taken_over' : 'waiting',
  }
}

/** A defensive cap on the agent's reason; the display clamps it to two lines. */
export const REASON_MAX = 200
