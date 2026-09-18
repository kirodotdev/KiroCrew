/**
 * The `decisions.preview` flag — read side, as a pure function over the config.
 *
 * Unlike the other Feature Previews in `FeaturePreviewsSection.tsx`, this one is
 * NOT a per-device `previewFlags.ts` key. The gate that will act on it runs in the
 * backend, which cannot read this browser's localStorage — so the flag has to be a
 * `config.json` value, written through `PATCH /api/config/kirocrew` like the
 * telemetry switch on the Privacy panel. That backend is not on `main` yet: today
 * nothing reads this value, and this module's whole job is to notice that and say
 * so rather than offer a switch against a field the gateway does not have.
 *
 * A backend that predates the `decisions` section answers the config GET without
 * one, and its PATCH allowlist would refuse the write. That is a real state of
 * this dashboard — the frontend ships ahead of the gateway it talks to whenever a
 * user updates one half first — so `supported` is derived rather than assumed,
 * and the card disables its switch instead of offering a write that returns 400.
 */

/** The config path the card's switch writes. */
export const DECISIONS_PREVIEW_PATH = 'decisions.preview'

/**
 * The three seams the preview reads at, in the order the card lists them.
 *
 * Hard-coded rather than enumerated from the config: these are the points the
 * shipped copy names, and a config that grows a fourth must not silently add a
 * row whose meaning this release's copy never explained.
 */
export const DECISION_POINTS = ['skills.select', 'skills.dedupe', 'cron.novelty'] as const

/** One read-only row: a point and the arm the config has it on. */
export interface DecisionPointArm {
  point: string
  arm: string
}

export interface DecisionsPreviewView {
  /** Whether this gateway's config carries a `decisions` section at all. */
  supported: boolean
  /** The stored flag. Only an exact `true` reads as on. */
  preview: boolean
  /** Points whose arm the config actually exposes; empty means render no rows. */
  arms: DecisionPointArm[]
}

const UNSUPPORTED: DecisionsPreviewView = { supported: false, preview: false, arms: [] }

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/**
 * Read the flag and the point arms out of a `GET /api/config/kirocrew` body.
 *
 * `undefined` — the query has not resolved, or it failed — reads as unsupported,
 * which is also how the card renders it: a switch offered against a config the
 * dashboard has not read yet would be guessing at its own current state.
 *
 * Only an exact `true` turns the preview on. A hand-edited `"true"` or `1` reads
 * as off, because this is an opt-in that sends message text off the machine and a
 * sloppy value is not consent.
 */
export function readDecisionsPreview(config: unknown): DecisionsPreviewView {
  const root = asRecord(config)
  if (!root) return UNSUPPORTED
  const decisions = asRecord(root.decisions)
  if (!decisions) return UNSUPPORTED

  const points = asRecord(decisions.points)
  const arms: DecisionPointArm[] = []
  for (const point of DECISION_POINTS) {
    const entry = points ? asRecord(points[point]) : null
    const arm = entry?.arm
    if (typeof arm === 'string' && arm) arms.push({ point, arm })
  }

  return { supported: true, preview: decisions.preview === true, arms }
}
