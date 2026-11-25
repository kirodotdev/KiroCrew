/**
 * App widgets (`contributes.widgets`): which widget a `<mcwidget use="<app>:<id>">`
 * tag names, and whether its JSON payload can be drawn.
 *
 * The gateway validates the manifest; this module re-checks only the shape it acts
 * on, because the app list may be stale. A widget resolves only for an enabled app
 * that declares it.
 *
 * @module appWidgets/appWidgetRegistry
 */

/** Mirrors `MAX_WIDGETS_PER_APP` in `apps/manifest_widgets.py`. */
export const MAX_WIDGETS_PER_APP = 16
/** Mirrors `MAX_WIDGET_TITLE` in `apps/manifest_widgets.py`. */
export const MAX_WIDGET_TITLE = 40
/** The largest payload drawn, in UTF-8 bytes of the trimmed tag body. */
export const MAX_WIDGET_PROPS_BYTES = 16_384
/** `<app>:<id>`: a kebab-case app name and a frame point id (`POINT_ID_RE`). */
const USE_RE = /^([a-z0-9]+(?:-[a-z0-9]+)*):([a-z][a-z0-9_-]{0,63})$/

export interface AppWidgetRef {
  appName: string
  widgetId: string
}

export interface ResolvedAppWidget extends AppWidgetRef {
  /** The name the user knows the app by. */
  appLabel: string
  title: string
}

interface AppLike {
  name?: string
  displayName?: string
  enabled?: boolean
  manifest?: { displayName?: string; contributes?: { widgets?: unknown } }
}

/** The app and widget a `use` attribute names, or null when it is not `<app>:<id>`. */
export function parseWidgetUse(use: string): AppWidgetRef | null {
  const m = USE_RE.exec(use)
  return m ? { appName: m[1], widgetId: m[2] } : null
}

/** Why a `use` tag names no drawable widget, when the app list says so. */
export type UnresolvedReason = 'not-installed' | 'app-off' | 'no-widget'

function findApp(apps: readonly AppLike[], appName: string): AppLike | undefined {
  return apps.find((a) => a && a.name === appName)
}

/** The name the user knows app `appName` by, from `apps`, or `appName` itself. */
export function appLabelFor(apps: readonly AppLike[], appName: string): string {
  const app = findApp(apps, appName)
  const label = (typeof app?.displayName === 'string' && app.displayName)
    || (typeof app?.manifest?.displayName === 'string' && app.manifest.displayName)
    || appName
  return String(label).slice(0, 80)
}

/** The declared widget `ref` names, from an enabled app in `apps`; else why not:
 *  `not-installed` when no app has that name, `app-off` when the app is turned off,
 *  `no-widget` when it declares no such widget. */
export function resolveAppWidget(apps: readonly AppLike[], ref: AppWidgetRef): ResolvedAppWidget | UnresolvedReason {
  const app = findApp(apps, ref.appName)
  if (!app) return 'not-installed'
  if (app.enabled === false) return 'app-off'
  const declared = app.manifest?.contributes?.widgets
  if (!Array.isArray(declared)) return 'no-widget'
  for (const raw of declared.slice(0, MAX_WIDGETS_PER_APP)) {
    if (!raw || typeof raw !== 'object') continue
    const w = raw as Record<string, unknown>
    if (w.id !== ref.widgetId) continue
    const title = typeof w.title === 'string' && w.title.length <= MAX_WIDGET_TITLE ? w.title : ''
    return { ...ref, appLabel: appLabelFor(apps, ref.appName), title }
  }
  return 'no-widget'
}

export type WidgetProps = Record<string, unknown>

/** Why a payload cannot be drawn, or the parsed object. */
export type PropsResult = { ok: true; props: WidgetProps } | { ok: false; reason: 'too-large' | 'not-object' }

/** Parse a tag body as the one JSON object a widget draws. */
export function parseWidgetProps(body: string): PropsResult {
  const text = body.trim()
  if (new TextEncoder().encode(text).length > MAX_WIDGET_PROPS_BYTES) return { ok: false, reason: 'too-large' }
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch {
    return { ok: false, reason: 'not-object' }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return { ok: false, reason: 'not-object' }
  return { ok: true, props: value as WidgetProps }
}
