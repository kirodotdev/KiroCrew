/**
 * The Decisions (Jev) card's read model.
 *
 * Two sources, deliberately, because the two values live in two different
 * places on the gateway and the split IS the security design:
 *
 * - **Consent** — whether Jev may be asked at all — is the KEYSTONE
 *   `decisions_consent.json`, read and written through `/api/decisions/consent`.
 *   It is not a config path: `config.json` is writable by an auto-approved agent
 *   shell, so a switch there could be flipped by a prompt-injected agent and the
 *   live config watcher would start sending message text off the machine. The
 *   keystone is mounted read-only in every sandbox and its only writer is the
 *   owner-only dashboard handler behind this card.
 * - **The sampling share** — `decisions.bucket` — comes from `config.json` through
 *   the ordinary config GET. It grants nothing on its own (it can only narrow what
 *   consent allows), so it stays a config value.
 *
 * Nothing is inferred from a legacy `decisions.preview` / `points.*.arm` section:
 * that experiment sampled for COMPARISON and is retired; reading its values as
 * consent would turn on egress from a value nobody wrote for it.
 */

/** The one point this build consumes an answer for. */
export const DECISIONS_LIVE_POINT = 'skills.select'

/** Config path of the sampling share; the only decisions value the config PATCH accepts. */
export const DECISIONS_BUCKET_PATH = 'decisions.bucket'

/** Bounds the backend clamps the sampling bucket to, restated for the reader. */
const BUCKET_MIN = 0
const BUCKET_MAX = 100

export interface DecisionsView {
  /**
   * Whether this gateway has the consent endpoint at all. An older gateway
   * (404 on the consent GET, or a config carrying only the retired `preview`
   * section) renders the switch disabled with the update notice.
   */
  supported: boolean
  /** The keystone's answer. Only an exact `true` reads as on. */
  enabled: boolean
  /**
   * Where a decision would be sent: the endpoint `config.json` names now. Shown
   * so the reader consents to an ADDRESS, not just to "sending".
   */
  configuredEndpoint: string
  /**
   * Consent was given, but for a different address than the config names now
   * (`provider.endpoint` was edited afterwards). Nothing is sent in this state;
   * the card says so and asks the owner to consent again.
   */
  endpointMoved: boolean
  /**
   * Sampling percentage worth PRINTING, or `null` when there is nothing to say.
   *
   * `null` covers the configs that mean "do not print a rate": the section or
   * field is absent (an older or hand-trimmed config), or it is not a whole
   * number in 0–100 (so the backend's own clamp decides, and this reader must not
   * guess which way). 100 IS printed: it is the shipped default, and "every
   * session" is the one share a reader deciding whether to consent most needs to
   * see.
   */
  bucket: number | null
  /**
   * Whether the owner consented to sending TOOL-CALL ARGUMENTS — the extra egress
   * category `tool.risk` needs, and the only thing that lets it run.
   *
   * Read from the keystone's own answer rather than inferred from `enabled`: a
   * consent recorded before this scope existed reads `false` here, which is
   * exactly the state its owner agreed to, and the second switch must draw that
   * rather than a value it guessed.
   */
  toolArgs: boolean
}

const UNSUPPORTED: DecisionsView = {
  supported: false,
  enabled: false,
  configuredEndpoint: '',
  endpointMoved: false,
  bucket: null,
  toolArgs: false,
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/**
 * The sampling rate out of a `GET /api/config/kirocrew` body, or `null` when the
 * config gives nothing printable.
 *
 * A percentage of 0 IS printable and is kept: "on, and sampling nobody" is a
 * state an operator can otherwise only discover by waiting for a log line that
 * never comes.
 */
export function readBucket(config: unknown): number | null {
  const root = asRecord(config)
  const decisions = root ? asRecord(root.decisions) : null
  const raw = decisions?.bucket
  if (typeof raw !== 'number' || !Number.isInteger(raw)) return null
  if (raw < BUCKET_MIN || raw > BUCKET_MAX) return null
  return raw
}

/**
 * Read consent out of a `GET /api/decisions/consent` body.
 *
 * `undefined` — the query has not resolved, or it failed (a 404 on an older
 * gateway included) — reads as unsupported, which is also how the card renders
 * it: a switch offered against a keystone the dashboard has not read yet would
 * be guessing at its own current state.
 *
 * Only an exact `true` turns the preview on. The backend writes nothing else,
 * and a hand-edited `"true"` or `1` in the keystone is refused there too; this
 * reader mirrors that so the card never shows "on" for a value the gate reads
 * as off.
 */
export function readConsent(body: unknown): Omit<DecisionsView, 'bucket'> {
  const root = asRecord(body)
  if (!root || !('enabled' in root)) {
    return {
      supported: false,
      enabled: false,
      configuredEndpoint: '',
      endpointMoved: false,
      toolArgs: false,
    }
  }
  const enabled = root.enabled === true
  const configuredEndpoint = typeof root.configured_endpoint === 'string' ? root.configured_endpoint : ''
  // `permits` is the server's own verdict (enabled AND same address). Read it
  // rather than re-deriving equality here, so the card and the gate cannot
  // disagree about whether anything is being sent.
  const endpointMoved = enabled && root.permits !== true
  // An exact `true`, like `enabled` above: this field decides whether a new
  // category of conversation content leaves the machine, so a truthy stand-in is
  // not a deliberate yes. An older gateway omits it entirely and reads as off.
  const toolArgs = root.tool_args === true
  return { supported: true, enabled, configuredEndpoint, endpointMoved, toolArgs }
}

/** Combine the two reads into the card's one view. */
export function readDecisions(consentBody: unknown, config: unknown): DecisionsView {
  const consent = readConsent(consentBody)
  if (!consent.supported) return UNSUPPORTED
  return { ...consent, bucket: readBucket(config) }
}
