/**
 * Shared duplicate-registration policy for the frontend extension seams.
 *
 * Every seam registrar (builtin pages, nav icons, theme branding, top-bar
 * widgets, panel shortcuts) resolves a key collision the same way: the core (or
 * first) registration wins and the duplicate is ignored. But a *silent* warn is
 * a trap — a later upstream sync that adds a core route/icon/theme/chord
 * colliding with a downstream registration would make the downstream
 * contribution vanish for end users with only a `console.warn` nobody watches
 * in production.
 *
 * So: fail LOUD where it can be caught (dev/test builds throw, so the collision
 * surfaces at build/test time), and degrade SAFE in production (warn + ignore,
 * so a shipped app never white-screens over a duplicate registration).
 *
 * Degrading safe is not the same as degrading SILENTLY, and this function cannot
 * close that gap on its own. A downstream edition compiles its own bundle, so the
 * dev/test throw never runs over its registrations — the first anyone hears of a
 * refused one is a `console.warn` in a shipped app. Making a refusal answerable
 * afterwards means remembering it, and remembering it is only useful to a seam
 * that has somewhere to SHOW it: today that is the builtin-page registry alone,
 * whose refused routes stay navigable and would otherwise render nothing. So the
 * record lives there, in `builtinRegistry.ts`, next to the miss path that reads
 * it — not here, where every other caller would pay for a store it has no
 * surface for. A seam that grows one can keep its own; this stays the shared
 * fail-loud/degrade-safe policy and nothing more.
 *
 * The refusal is still not thrown at registration time: a throw there takes the
 * whole dashboard down over one bad entry, which is worse than the entry being
 * missing.
 */
export function reportSeamCollision(scope: string, message: string): void {
  const full = `[${scope}] ${message}`
  // import.meta.env.DEV is true under Vite dev + vitest, false in prod builds.
  if (import.meta.env?.DEV) {
    throw new Error(
      `${full}. Extension-seam collisions must be resolved before release ` +
        `(this throws in dev/test, warns in production).`,
    )
  }
  // eslint-disable-next-line no-console
  console.warn(full)
}
