/**
 * The pages of the apps that ship in the package, for the find_ui index.
 *
 * Read at index BUILD time from each packaged manifest (`app.json`) by
 * scripts/gen-ui-index.mjs; find_ui itself reads only the generated index.
 * Everything here is data from the manifests: no app is named in this file.
 *
 * Runtime-installed apps are not packaged, so they are not indexed here.
 */

/** Id prefix of an app page location: `apppage.<manifest name>.<route slug>`. */
export const APP_PAGE_PREFIX = 'apppage.'
/** The route arm (App.tsx) that serves every built-in app's page. */
export const BUILTIN_APP_ARM = '/:builtinApp/*'

const NAME_RE = /^[a-z0-9][a-z0-9-]*$/

/**
 * The catalog keys a built-in app's manifest copy is localised under
 * (`APP_MANIFEST_KEY` in src/components/appstore/appManifest.ts, kept in step by
 * scripts/check-app-manifest-sync.mjs): `apps.<camelCaseId>.manifest.<field>`.
 * That table holds one page label per app, which the rail shows for the app's
 * first page (`appNavTarget`), so only the first page takes that key.
 */
function manifestKey(name, field) {
  const [head, ...rest] = name.split('-')
  const ns = head + rest.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join('')
  return `apps.${ns}.manifest.${field}`
}

function routeSlug(route) {
  return route.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'root'
}

/**
 * `manifests`: `[{ rel, text }]`, one per packaged `app.json`, in a stable
 * order. Returns `{ pages, errors }`; each page is
 * `{ id, app, appLabel, route, label, rel, labelKey, appLabelKey }`.
 */
export function appPagesFromManifests(manifests) {
  const pages = []
  const errors = []
  const ids = new Set()
  for (const { rel, text } of manifests) {
    let m
    try {
      m = JSON.parse(text)
    } catch (e) {
      errors.push(`${rel}: not valid JSON (${e.message})`)
      continue
    }
    const name = m?.name
    if (typeof name !== 'string' || !NAME_RE.test(name)) {
      errors.push(`${rel}: 'name' must be a lowercase kebab-case string`)
      continue
    }
    const appLabel = typeof m.displayName === 'string' && m.displayName.trim() ? m.displayName.trim() : name
    const list = m.ui?.pages ?? []
    if (!Array.isArray(list)) {
      errors.push(`${rel}: 'ui.pages' must be a list`)
      continue
    }
    for (const [i, p] of list.entries()) {
      const where = `${rel}: ui.pages[${i}]`
      const route = p?.route
      const label = typeof p?.label === 'string' ? p.label.trim() : ''
      if (typeof route !== 'string' || !route.startsWith('/')) {
        errors.push(`${where}: 'route' must be an absolute path`)
        continue
      }
      if (!label) {
        errors.push(`${where}: 'label' must be a nonblank string`)
        continue
      }
      const id = `${APP_PAGE_PREFIX}${name}.${routeSlug(route)}`
      if (ids.has(id)) {
        errors.push(`${where}: a second page of '${name}' maps to location id '${id}'`)
        continue
      }
      ids.add(id)
      pages.push({
        id, app: name, appLabel, route, label, rel,
        labelKey: i === 0 ? manifestKey(name, 'page_label') : null,
        appLabelKey: manifestKey(name, 'display_name'),
      })
    }
  }
  return { pages, errors }
}
