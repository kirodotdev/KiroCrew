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
