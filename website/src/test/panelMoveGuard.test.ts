/**
 * The move guard that the gateway puts into a panel's page (`browser_harness/panel_page.py`, `MOVE_JS`), taken from
 * the gateway's own source and run here against a stand-in for the browser's `navigation`: an in-page move to a
 * path off the list is cancelled before it happens, and nothing else is touched.
 */
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LIVE_SHAPES, SHUT_SHAPES } from '../pages/xr/workspace/streamPath'
import { panelScriptSource } from './panelGuard'

type Holder = { navigation?: EventTarget; __kcPanelShapes?: readonly string[]; __kcPanelShut?: readonly string[]; __kcPanelLaunch?: readonly string[]; __kcPanelMoves?: boolean; __kcPanelGuarded?: (what: string) => void }
const holder = globalThis as Holder
const said = vi.fn()

/** The names of the parameters that make the dashboard act with no press, as the gateway holds them. */
const LAUNCH = [...(/^LAUNCH_PARAMS = frozenset\(\{([^}]*)\}\)/m.exec(readFileSync('../src/kiro_crew/browser_harness/panel_page.py', 'utf-8'))?.[1] ?? '').matchAll(/"([a-z]+)"/g)].map(m => m[1])

/** Run the gateway's script with `shapes` and the acting parameters in front of it, as the hub does, and hand back what the browser would tell it. */
function install(shapes: readonly string[] | undefined): EventTarget {
  const navigation = new EventTarget()
  holder.navigation = navigation
  holder.__kcPanelShapes = shapes
  holder.__kcPanelLaunch = shapes ? LAUNCH : undefined
  holder.__kcPanelGuarded = said
  new Function(panelScriptSource('MOVE_JS'))()
  return navigation
}

/** One move as the browser tells it. Returns whether the script cancelled it. */
function moved(navigation: EventTarget, path: string, { type = 'push', sameDocument = true, cancelable = true, origin = location.origin } = {}): boolean {
  const event = Object.assign(new Event('navigate', { cancelable }), { navigationType: type, destination: { url: origin + path, sameDocument } })
  navigation.dispatchEvent(event)
  return event.defaultPrevented
}

beforeEach(() => { said.mockReset() })
afterEach(() => { delete holder.navigation; delete holder.__kcPanelShapes; delete holder.__kcPanelShut; delete holder.__kcPanelLaunch; delete holder.__kcPanelMoves; delete holder.__kcPanelGuarded })

describe('the move guard of a panel\'s page', () => {
  it('with the gateway\'s guard off, lets a move to any page through but the few that stay shut, and refuses a token', () => {
    // As the hub hands it over with `browser.xr_pages_guarded` off: the shut patterns, and `token` alone.
    const navigation = new EventTarget()
    holder.navigation = navigation
    holder.__kcPanelShut = SHUT_SHAPES
    holder.__kcPanelLaunch = ['token']
    holder.__kcPanelGuarded = said
    new Function(panelScriptSource('MOVE_JS'))()
    for (const path of ['/settings/connections', '/capabilities?tab=knowledge', '/apps/notes', '/chat?autoSend=1', '/projects?applied=t-1&autoRun=1', '/settings?tab=connections', '/schedule', '/artifacts/remote-notes']) expect(moved(navigation, path), path).toBe(false)
    expect(said).not.toHaveBeenCalled()
    for (const path of ['/xr', '/XR/', '/%78r', '/xr?view=pages', '/deploy', '/artifacts/deploy', '/artifacts/remote/github/42', '/settings/people', '/settings/People/invite', '/settings/instances', '/instances', '/settings?tab=people', '/Settings/?tab=instances', '/chat?token=abc', '/chat?autoSend=1&TOKEN=abc', '/chat/../xr']) {
      said.mockReset()
      expect(moved(navigation, path), path).toBe(true)
      expect(said).toHaveBeenCalledExactlyOnceWith('move')
    }
  })

  it('cancels a move inside the page to a path off the list, and says so', () => {
    const navigation = install(LIVE_SHAPES)
    for (const path of ['/settings/connections', '/settings/people', '/settings/instances/add', '/capabilities?tab=knowledge', '/deploy', '/artifacts/deploy', '/apps/notes', '/xr', '/chat/../capabilities', '/']) {
      said.mockReset()
      expect(moved(navigation, path), path).toBe(true)
      expect(said).toHaveBeenCalledExactlyOnceWith('move')
    }
    // A move that replaces the entry is stopped as one that adds an entry is.
    expect(moved(navigation, '/settings/connections', { type: 'replace' })).toBe(true)
  })

  it('lets a move to a page on the list through, with whatever query it carries', () => {
    const navigation = install(LIVE_SHAPES)
    for (const path of ['/artifacts', '/artifacts/report-1', '/chat/dashboard-ab12?sid=1#row-9', '/settings/browser', '/settings/agent/models', '/projects/p1/tasks/t-2', '/members', '/chat?new=1&newSession=1', '/projects?applied=t-1']) expect(moved(navigation, path), path).toBe(false)
    expect(said).not.toHaveBeenCalled()
  })

  it('cancels a move to a page on the list whose address makes the dashboard act with no press', () => {
    expect(LAUNCH.sort()).toEqual(['autorun', 'autosend', 'token'])
    const navigation = install(LIVE_SHAPES)
    // A note that an agent raised carries a button with an address of the agent's choosing: one tap is this move.
    for (const path of [
      '/projects?applied=t-1&autoRun=true', '/tasks?autoRun=true', '/chat?token=abc', '/chat/dashboard-ab12?autoSend=1', '/chat?autoSend=1&newSession=1',
      // As the page would read them: in any case, with a coded letter, given twice, with spaces round the name.
      '/projects?applied=t-1&AUTORUN=true', '/projects?applied=t-1&auto%52un=true', '/projects?autoRun=false&autoRun=true', '/chat?+token+=abc',
    ]) {
      said.mockReset()
      expect(moved(navigation, path), path).toBe(true)
      expect(moved(navigation, path, { type: 'replace' }), path).toBe(true)
      expect(said).toHaveBeenCalledWith('move')
    }
    // The page splits its query at `&` alone and does not read the part after `#`: neither of these is the name.
    said.mockReset()
    expect(moved(navigation, '/projects?applied=t-1;autoRun=true')).toBe(false)
    expect(moved(navigation, '/projects?applied=t-1#autoRun=true')).toBe(false)
    expect(said).not.toHaveBeenCalled()
  })

  it('cancels a move to the bare Settings path with a tab that the list leaves out', () => {
    const navigation = install(LIVE_SHAPES)
    // Settings draws the tab its query names when its path names none, before it rewrites its address.
    for (const path of [
      '/settings?tab=connections', '/settings?tab=people', '/settings?tab=instances',
      '/settings?tab=%63onnections', '/settings?tab=peopl%65', '/settings?q=1&tab=inst%61nces',
      // Given twice, every value must pass, whichever the page would take.
      '/settings?tab=browser&tab=connections', '/settings?tab=people&tab=browser',
      '/settings?TAB=connections', '/settings?tab=Connections', '/settings?tab=connections/x', '/settings?tab=',
    ]) {
      said.mockReset()
      expect(moved(navigation, path), path).toBe(true)
      expect(moved(navigation, path, { type: 'replace' }), path).toBe(true)
      expect(said).toHaveBeenCalledWith('move')
    }
    said.mockReset()
    // A tab on the list passes; the path's tab wins over the query's; no other page reads a tab this way.
    for (const path of ['/settings?tab=browser', '/settings?tab=agent&tab=browser', '/settings/x?tab=connections', '/settings/browser?tab=people', '/chat?tab=connections', '/settings?q=1;tab=connections', '/settings#tab=connections']) expect(moved(navigation, path), path).toBe(false)
    expect(said).not.toHaveBeenCalled()
  })

  it('leaves a load, a step in the history and a move it may not cancel to the gateway', () => {
    const navigation = install(LIVE_SHAPES)
    expect(moved(navigation, '/capabilities', { sameDocument: false })).toBe(false)
    expect(moved(navigation, '/capabilities', { type: 'traverse' })).toBe(false)
    expect(moved(navigation, '/capabilities', { type: 'reload' })).toBe(false)
    expect(moved(navigation, '/capabilities', { cancelable: false })).toBe(false)
    expect(moved(navigation, '/capabilities', { origin: 'https://elsewhere.example' })).toBe(false)
    expect(said).not.toHaveBeenCalled()
  })

  it('lets no move through when it was given no list', () => {
    const navigation = install(undefined)
    expect(moved(navigation, '/chat')).toBe(true)
    expect(moved(navigation, '/settings')).toBe(true)
  })

  it('listens once, however often it is run', () => {
    const navigation = install(LIVE_SHAPES)
    new Function(panelScriptSource('MOVE_JS'))()
    expect(moved(navigation, '/capabilities')).toBe(true)
    expect(said).toHaveBeenCalledTimes(1)
  })
})
