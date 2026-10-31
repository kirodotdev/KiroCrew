import { describe, it, expect, vi } from 'vitest'
import { matchRoutes } from 'react-router-dom'
import { popoutWindowName, buildPopoutUrl, openPopout, __resetForTests } from '../utils/appPopout'
import {
  APP_POPOUT_PREFIX,
  appRouteOf,
  appPopoutRouteOf,
  appPopoutRouterBasename,
  appPopoutPath,
  routerParamForm,
} from '../utils/appPopoutRoute'

/**
 * Pure-logic tests for the app pop-out helpers: which dashboard paths count as
 * an app (the main window's control shows there and nowhere else), how a
 * pop-out document path names its app, the router basename the pop-out window
 * mounts at, and the window-name + URL shape. The BroadcastChannel/heartbeat
 * wiring is shared with the chat and artifact pop-outs via popoutController and
 * covered there.
 */
const builtins = new Set(['/dev-fleet', '/aws-control', '/worlds'])
const hasBuiltin = (route: string) => builtins.has(route)

describe('appRouteOf (main window: is this path an app page?)', () => {
  it('names a builtin app by its first segment, ignoring the app’s own sub-path', () => {
    expect(appRouteOf('/dev-fleet', hasBuiltin)).toBe('/dev-fleet')
    expect(appRouteOf('/aws-control/usage', hasBuiltin)).toBe('/aws-control')
    expect(appRouteOf('/aws-control/usage/deeper', hasBuiltin)).toBe('/aws-control')
  })

  it('names an AppHost-routed installed app by its /apps/<name> route', () => {
    expect(appRouteOf('/apps/my-tool', hasBuiltin)).toBe('/apps/my-tool')
    // These are valid installed-app names because their store routes need a third segment.
    expect(appRouteOf('/apps/detail', hasBuiltin)).toBe('/apps/detail')
    expect(appRouteOf('/apps/migrate', hasBuiltin)).toBe('/apps/migrate')
  })

  it('is null for the App Store’s own pages, which live under /apps too', () => {
    expect(appRouteOf('/apps', hasBuiltin)).toBeNull()
    expect(appRouteOf('/apps/library', hasBuiltin)).toBeNull()
    expect(appRouteOf('/apps/detail/my-tool', hasBuiltin)).toBeNull()
    expect(appRouteOf('/apps/migrate/my-tool', hasBuiltin)).toBeNull()
    expect(appRouteOf('/apps/-/updates', hasBuiltin)).toBeNull()
  })

  it('is null for every core page and for a first segment the registry does not know', () => {
    expect(appRouteOf('/', hasBuiltin)).toBeNull()
    expect(appRouteOf('/chat', hasBuiltin)).toBeNull()
    expect(appRouteOf('/settings/instances', hasBuiltin)).toBeNull()
    expect(appRouteOf('/not-an-app', hasBuiltin)).toBeNull()
  })

  it('does not treat a deeper /apps path as an installed app (the /apps/:name arm is exact)', () => {
    expect(appRouteOf('/apps/my-tool/view', hasBuiltin)).toBeNull()
  })
})

describe('appPopoutRouteOf (pop-out window: which app is this document for?)', () => {
  it('reads the app route out of the document path, dropping the app’s sub-path', () => {
    expect(appPopoutRouteOf('/popout/app/dev-fleet')).toBe('/dev-fleet')
    expect(appPopoutRouteOf('/popout/app/aws-control/usage')).toBe('/aws-control')
    expect(appPopoutRouteOf('/popout/app/apps/my-tool')).toBe('/apps/my-tool')
    expect(appPopoutRouteOf('/popout/app/apps/my-tool/view')).toBe('/apps/my-tool')
  })

  it('is null outside the app pop-out prefix, and for the bare prefix', () => {
    expect(appPopoutRouteOf('/popout/chat/slug')).toBeNull()
    expect(appPopoutRouteOf('/popout/artifact/x')).toBeNull()
    expect(appPopoutRouteOf('/popout/app')).toBeNull()
    expect(appPopoutRouteOf('/popout/app/')).toBeNull()
    expect(appPopoutRouteOf('/dev-fleet')).toBeNull()
    expect(appPopoutRouteOf('/popout/application/x')).toBeNull()
  })

  it('does not validate: a bogus first segment is still a route for the frame to refuse', () => {
    expect(appPopoutRouteOf('/popout/app/chat')).toBe('/chat')
    expect(appPopoutRouteOf('/popout/app/apps')).toBe('/apps')
  })
})

describe('routerParamForm (a route read the way the router reads its params)', () => {
  it('leaves a plain route as it is', () => {
    expect(routerParamForm('/apps/my-tool')).toBe('/apps/my-tool')
    expect(routerParamForm('/dev-fleet')).toBe('/dev-fleet')
  })

  it('decodes each segment', () => {
    expect(routerParamForm('/apps/a%20b')).toBe('/apps/a b')
    expect(routerParamForm('/dev%2Dfleet')).toBe('/dev-fleet')
  })

  it('turns an encoded slash into a slash, as the params do', () => {
    expect(routerParamForm('/apps/foo%2Fbar')).toBe('/apps/foo/bar')
    expect(routerParamForm('/apps/foo%2fbar')).toBe('/apps/foo/bar')
  })

  it('leaves a path with a malformed escape undecoded, but still turns %2F into a slash', () => {
    expect(routerParamForm('/apps/%E0%A4%A')).toBe('/apps/%E0%A4%A')
    expect(routerParamForm('/apps/%E0%A4%A%2Fx')).toBe('/apps/%E0%A4%A/x')
  })

  // Pinned against the router itself, so an upgrade that changes how params
  // are decoded fails here rather than in a pop-out window.
  it('agrees with the params react-router reads for the frame’s two arms', () => {
    const arms = [{ path: '/apps/:name' }, { path: '/:builtinApp/*' }]
    for (const path of ['/apps/my-tool', '/apps/a%20b', '/apps/foo%2Fbar', '/apps/foo%2fbar', '/apps/%E0%A4%A',
      '/apps/%E0%A4%A%2Fx', '/dev%2Dfleet', '/apps%2Ffoo', '/apps/%252F', "/apps/!~*'()"]) {
      const params = matchRoutes(arms, path)?.[0].params ?? {}
      const here = params.name !== undefined ? `/apps/${params.name}` : `/${params.builtinApp ?? ''}`
      expect(routerParamForm(path), path).toBe(here)
    }
  })
})

describe('appPopoutRouterBasename', () => {
  it('mounts the router at /popout/app inside an app pop-out window', () => {
    expect(appPopoutRouterBasename('/popout/app/dev-fleet', '')).toBe('/popout/app')
    expect(appPopoutRouterBasename('/popout/app/aws-control/usage', '')).toBe('/popout/app')
  })

  it('keeps the build base in front of the prefix in a sub-path build', () => {
    expect(appPopoutRouterBasename('/proxy/kc/popout/app/dev-fleet', '/proxy/kc')).toBe('/proxy/kc/popout/app')
  })

  it('falls back to the ordinary router basename everywhere else', () => {
    expect(appPopoutRouterBasename('/chat', '')).toBeUndefined()
    expect(appPopoutRouterBasename('/popout/chat/slug', '')).toBeUndefined()
    expect(appPopoutRouterBasename('/popout/app', '')).toBeUndefined()
    expect(appPopoutRouterBasename('/proxy/kc/chat', '/proxy/kc')).toBe('/proxy/kc')
    expect(appPopoutRouterBasename('/proxy/kc/popout/chat/x', '/proxy/kc')).toBe('/proxy/kc')
  })

  it('does not read a /popout/app path that sits outside the build base as a pop-out', () => {
    expect(appPopoutRouterBasename('/popout/app/dev-fleet', '/proxy/kc')).toBe('/proxy/kc')
  })
})

describe('appPopoutPath / buildPopoutUrl', () => {
  it('places the app route under the pop-out prefix', () => {
    expect(appPopoutPath('/dev-fleet')).toBe(`${APP_POPOUT_PREFIX}/dev-fleet`)
    expect(appPopoutPath('/apps/my-tool')).toBe('/popout/app/apps/my-tool')
  })

  it('builds an absolute same-origin URL for window.open', () => {
    expect(buildPopoutUrl('/dev-fleet')).toBe(`${window.location.origin}/popout/app/dev-fleet`)
    expect(buildPopoutUrl('/apps/my-tool')).toBe(`${window.location.origin}/popout/app/apps/my-tool`)
  })

  it('opens at the initial page and query when one is given, at the app root otherwise', () => {
    const origin = window.location.origin
    expect(buildPopoutUrl('/aws-control', '/aws-control/usage?tab=x')).toBe(`${origin}/popout/app/aws-control/usage?tab=x`)
    expect(buildPopoutUrl('/apps/my-tool', '/apps/my-tool/deep?q=1')).toBe(`${origin}/popout/app/apps/my-tool/deep?q=1`)
    expect(buildPopoutUrl('/aws-control')).toBe(`${origin}/popout/app/aws-control`)
    expect(buildPopoutUrl('/aws-control', '')).toBe(`${origin}/popout/app/aws-control`)
  })

  it('drops a hash, and falls back to the root for a path the window would not pin to the route', () => {
    const origin = window.location.origin
    expect(buildPopoutUrl('/aws-control', '/aws-control/usage?tab=x#frag')).toBe(`${origin}/popout/app/aws-control/usage?tab=x`)
    expect(buildPopoutUrl('/aws-control', '/chat?sid=x')).toBe(`${origin}/popout/app/aws-control`)
    expect(buildPopoutUrl('/apps/my-tool', '/apps/other-tool')).toBe(`${origin}/popout/app/apps/my-tool`)
  })

  it('opens the window at the initial path under the ROUTE’s window name (dedupe stays by route)', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({ closed: false, focus: () => {} } as unknown as Window)
    try {
      openPopout('/aws-control', '/aws-control/usage?tab=x')
      expect(open).toHaveBeenCalledWith(
        `${window.location.origin}/popout/app/aws-control/usage?tab=x`,
        popoutWindowName('/aws-control'),
        expect.any(String),
      )
    } finally {
      open.mockRestore()
      __resetForTests()
    }
  })
})

describe('popoutWindowName', () => {
  it('is stable and filesystem-safe for a route', () => {
    expect(popoutWindowName('/dev-fleet')).toBe('mc-app-popout-_dev-fleet')
    expect(popoutWindowName('/apps/my-tool')).toBe('mc-app-popout-_apps_my-tool')
  })

  it('does not collide with the chat, artifact or terminal pop-out names', () => {
    expect(popoutWindowName('/chat')).not.toMatch(/^mc-popout-/)
    expect(popoutWindowName('/artifact')).not.toMatch(/^mc-artifact-popout-/)
  })
})
