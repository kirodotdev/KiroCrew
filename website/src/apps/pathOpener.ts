/**
 * "Open in <app>": the installed app page a surface showing a file can hand that
 * file's absolute path to, and the link that does it.
 *
 * A builtin page opts in by declaring `opensPathParam` on its `BuiltinAppEntry`:
 * the query parameter it reads the path from (`<route>?<param>=<path>`). Nothing
 * here names an app. With no entry declaring one, there is no target and every
 * surface that asks offers no row.
 *
 * The page must be served at its NATIVE route, because only the registered
 * component reads the parameter. An orphaned app (routed to its migration page),
 * or one an edition re-homes under AppHost, would swallow it and land the reader
 * on a page that opened nothing, so neither is a target. Eligibility and label
 * come from `appNavTarget`, the derivation the left rail and the command palette
 * use, so a surface cannot offer an app those two hide: a disabled app has
 * nowhere to go.
 *
 * Pure, so the rule is pinned by unit test rather than by mounting a menu.
 */
import { appNavTarget, type AppNavRecord } from '../appNav'
import { BUILTIN_COMPONENT_REGISTRY, type BuiltinAppEntry } from './builtinRegistry'

/** A query parameter name that needs no encoding to sit in the link as written. */
const PATH_PARAM_RE = /^[A-Za-z0-9_-]+$/

/** Where a path can be opened: the page's label, its route, and its parameter. */
export interface PathOpenerTarget {
  readonly label: string
  readonly route: string
  readonly param: string
}

/**
 * The first registered page that declares a path parameter and whose app is
 * enabled and routed natively in `apps`, or `null` when there is none.
 * Registration order decides between two candidates. `registry` is the live
 * builtin registry unless a test hands in its own.
 */
export function pathOpenerTarget(
  apps: readonly AppNavRecord[],
  registry: Readonly<Record<string, BuiltinAppEntry>> = BUILTIN_COMPONENT_REGISTRY,
): PathOpenerTarget | null {
  for (const [route, entry] of Object.entries(registry)) {
    const param = entry.opensPathParam
    if (!param || !PATH_PARAM_RE.test(param)) continue
    const app = apps.find(a => a.name === entry.appId)
    const target = app ? appNavTarget(app) : null
    if (target?.route === route) return { label: target.label, route, param }
  }
  return null
}

/**
 * The URL that opens `path` at `target`.
 *
 * Callers gate on `isAbsolutePath(path)` first: an app backend resolves a
 * relative path against its own process's working directory, which no link
 * author can know, so a relative value would open whatever happens to sit there.
 */
export function pathOpenerLink(target: Pick<PathOpenerTarget, 'route' | 'param'>, path: string): string {
  return `${target.route}?${target.param}=${encodeURIComponent(path)}`
}
