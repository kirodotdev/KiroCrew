import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { isValidElement } from 'react'
import { describe, expect, it } from 'vitest'
import { i18nT } from '../../../i18n/t'
import { BUILTIN_SURFACE_NAV } from '../../../surfaces/surfaceData'
import { CLOSE_MARK, calmAfterComing, closeSize, closes, closesAtRest, pressCloses, shownAfterPress } from './closeMark'
import { getAdvertisedSurfaces, surfaceLabel } from '../../../surfaces/registry'
import { surfaceSwitchedOn } from '../../../surfaces/surfaceData'
import { PAGE_MENU, REDIRECTS, menuLabel, opensOnPanel, pagesToOpen } from './pageMenu'
import { listedPath, livePath } from './streamPath'

describe('the pages a person can open', () => {
  it('are eight, in the order the round ruled', () => {
    expect(PAGE_MENU.map(page => page.path)).toEqual(['/chat', '/members', '/artifacts', '/schedule', '/projects', '/sessions', '/notifications', '/settings'])
  })

  it('are each a page that a panel may hold', () => {
    // Each is on the list that opens with the gateway's guard on too, so the plus works in both ways.
    for (const page of PAGE_MENU) { expect(livePath(page.path), page.path).toBe(page.path); expect(listedPath(page.path), page.path).toBe(true) }
  })

  it('carry the dashboard\'s own words: no label is new', () => {
    expect(PAGE_MENU.map(page => i18nT(page.labelKey))).toEqual(['Chat', 'Bots', 'Artifacts', 'Schedule', 'Task Runner', 'All sessions', 'Notifications', 'Settings'])
    // Where the rail has the page, the label is the rail's own. The chat is the one that is not: the rail calls it
    // "Sessions", which here is the list of all sessions, so it carries the name of its own tab.
    const rail = new Map(Object.values(BUILTIN_SURFACE_NAV).map(surface => [surface.route as string, surface.labelKey as string]))
    for (const page of PAGE_MENU) {
      if (page.path === '/chat' || !rail.has(page.path)) continue
      expect(page.labelKey, page.path).toBe(rail.get(page.path))
    }
    expect(rail.get('/chat')).toBe('nav.sessions')
  })

  it('each have an icon of the dashboard\'s own', () => {
    for (const page of PAGE_MENU) expect(isValidElement(page.icon), page.path).toBe(true)
  })
})

describe('the pages that the plus lists', () => {
  const icon = <span />
  const nav = (route: string, more: Record<string, unknown> = {}) => ({ route, label: route.slice(1), labelKey: undefined, icon, ...more })

  it('is the short list while the panels are guarded, and until the gateway has said which way holds', () => {
    expect(pagesToOpen('guarded', {})).toBe(PAGE_MENU)
    expect(pagesToOpen(null, {})).toBe(PAGE_MENU)
  })

  it('is the dashboard\'s own navigation while the panels are open: its pages, its names, its order', () => {
    const listed = pagesToOpen('open', {})
    const own = getAdvertisedSurfaces().filter(s => surfaceSwitchedOn(s, {}) && !s.route.includes('?'))
    expect(own.length).toBeGreaterThan(6)
    expect(listed.slice(0, own.length).map(page => page.path)).toEqual(own.map(s => s.route))
    expect(listed.slice(0, own.length).map(menuLabel)).toEqual(own.map(s => surfaceLabel(s)))
    for (const page of listed) { expect(livePath(page.path), page.path).toBe(page.path); expect(isValidElement(page.icon), page.path).toBe(true) }
    // The rail's own name for the chat is "Sessions"; the pages that the short list did not have are there.
    expect(menuLabel(listed[0])).toBe('Sessions')
    expect(listed.map(page => page.path)).toEqual(expect.arrayContaining(['/capabilities']))
    // After them, the short list's rows that the navigation does not name. No page is listed twice.
    expect(listed.slice(own.length).map(page => page.path)).toEqual(['/sessions', '/notifications'])
    expect(new Set(listed.map(page => page.path)).size).toBe(listed.length)
  })

  it('takes a page that the dashboard gains with no change here, by its own name', () => {
    const listed = pagesToOpen('open', {}, [nav('/chat'), nav('/reports', { label: 'Reports' })])
    expect(listed.map(page => page.path).slice(0, 2)).toEqual(['/chat', '/reports'])
    expect(menuLabel(listed[1])).toBe('Reports')
  })

  it('lists no page that stays shut in both ways, and no address with a query', () => {
    const shut = ['/xr', '/deploy', '/artifacts/deploy', '/settings/people', '/settings/instances', '/instances']
    const listed = pagesToOpen('open', {}, [nav('/chat'), ...shut.map(route => nav(route)), nav('/capabilities?tab=skills')])
    expect(listed.map(page => page.path)).toEqual(['/chat', ...PAGE_MENU.map(page => page.path).filter(path => path !== '/chat')])
  })

  it('leaves out a page whose own setting has it switched off, as the rail does', () => {
    const channels = nav('/channels', { configSwitch: 'dashboard.channels_enabled', configSwitchDefault: false })
    const paths = (config: unknown) => pagesToOpen('open', config, [channels]).map(page => page.path)
    expect(paths({})).not.toContain('/channels')
    expect(paths({ dashboard: { channels_enabled: true } })).toContain('/channels')
    // The Task Runner is on the short list too: switched off, it does not come back by that list.
    expect(pagesToOpen('open', {}).map(page => page.path)).toContain('/projects')
    expect(pagesToOpen('open', { taskrunner: { enabled: false } }).map(page => page.path)).not.toContain('/projects')
  })
})

describe('a row that goes on to another page', () => {
  it('is the dashboard\'s own list of redirects, read from App.tsx', () => {
    const app = readFileSync(resolve(process.cwd(), 'src/App.tsx'), 'utf8')
    const found = Object.fromEntries([...app.matchAll(/<Route path="(\/[^"*]*)" element=\{<Navigate to="([^"]+)"/g)].map(m => [m[1], m[2]]))
    expect(REDIRECTS).toEqual(found)
  })

  it('is listed only when the page that it goes on to may stand on a panel too', () => {
    expect(opensOnPanel('/instances')).toBe(false)
    expect(opensOnPanel('/artifacts/deploy')).toBe(false)
    expect(opensOnPanel('/agents')).toBe(true)
    expect(opensOnPanel('/knowledge')).toBe(true)
    expect(opensOnPanel('/chat')).toBe(true)
    expect(opensOnPanel('https://example.com/x')).toBe(false)
    expect(opensOnPanel('//example.com/x')).toBe(false)
  })
})

describe('the close mark of a panel', () => {
  it('says a press closes, which is when the mark is drawn white: never in the calm second after it came by itself (D50, F2)', () => {
    // A side page he turned to: it became the page in front at 5000, its mark lit with it.
    expect(pressCloses(5000, 5000, 5272)).toBe(false)
    expect(pressCloses(5000, 5000, 5000 + CLOSE_MARK.restCalmMs - 1)).toBe(false)
    expect(pressCloses(5000, 5000, 5000 + CLOSE_MARK.restCalmMs)).toBe(true)
    // A page long in front: its mark at rest closes on the first press, lit just now or not.
    expect(pressCloses(1000, 9000, 9001)).toBe(true)
    // A mark that is not the page in front's closes once it was seen 150 ms.
    expect(pressCloses(null, 9000, 9000 + CLOSE_MARK.shownMs - 1)).toBe(false)
    expect(pressCloses(null, 9000, 9000 + CLOSE_MARK.shownMs)).toBe(true)
  })
  it('closes on the first tap when it shows at rest, but not in the second after it came by itself', () => {
    expect(closesAtRest(null, 9000)).toBe(false)
    expect(closesAtRest(5000, 5000 + CLOSE_MARK.restCalmMs - 1)).toBe(false)
    expect(closesAtRest(5000, 5000 + CLOSE_MARK.restCalmMs)).toBe(true)
    // The pages stood still again at 5500: the second counts from then.
    expect(closesAtRest(5000, 6200, 5500)).toBe(false)
    expect(closesAtRest(5000, 6500, 5500)).toBe(true)
  })

  it('takes no tap in the second after it came by itself, also when a resting mouse lit it as it came', () => {
    // It came at 5000 and the mouse that rests there lit it at 5050: a click at 5450 is in its first second.
    expect(calmAfterComing(5000, 5050, 5450)).toBe(true)
    expect(calmAfterComing(5000, 5050, 6200)).toBe(false)
    // Lit by the pointer before it came: it did not come by itself.
    expect(calmAfterComing(5000, 4000, 5450)).toBe(false)
    // Lit by the turn a moment before the page in front was looked for: it came by itself.
    expect(calmAfterComing(5000, 4800, 5450)).toBe(true)
    expect(calmAfterComing(null, 5050, 5450)).toBe(false)
  })

  it('closes the page only when it was there to be seen before the press', () => {
    expect(closes(null, 5000)).toBe(false)
    expect(closes(5000, 5000)).toBe(false)
    expect(closes(5000, 5000 + CLOSE_MARK.shownMs - 1)).toBe(false)
    expect(closes(5000, 5000 + CLOSE_MARK.shownMs)).toBe(true)
  })

  it('closes no page by a mark that was lit before the pages last stood still, and counts its light anew from that press', () => {
    // The flat preview moved the pages until 6000. A mark that was lit at 5800 was lit for where they stood before.
    expect(closes(5800, 6400, 6000)).toBe(false)
    expect(shownAfterPress(5800, 6400, 6000)).toBe(6400)
    // The next press on it, once it has been seen for long enough, closes.
    expect(closes(6400, 6400 + CLOSE_MARK.shownMs - 1, 6000)).toBe(false)
    expect(closes(6400, 6400 + CLOSE_MARK.shownMs, 6000)).toBe(true)
    // A mark that was lit when the pages stood still, or after, is what it always is, and a press leaves its count alone.
    expect(closes(6000, 6000 + CLOSE_MARK.shownMs, 6000)).toBe(true)
    expect(shownAfterPress(6000, 6400, 6000)).toBe(6000)
    expect(shownAfterPress(null, 6400, 6000)).toBe(null)
    // In a session nothing moves the pages by itself: no time is given, and the rule is the one above.
    expect(shownAfterPress(5000, 6400)).toBe(5000)
  })

  it('takes a press on 44 pixels of the screen at least, and is drawn at 20 at least, however thin the edge is there', () => {
    // An upright phone at the start view: the edge is 0.052 m thick, and one pixel of the screen covers 4.4 mm of the panel.
    const phone = closeSize(0.052, 0.0044)
    expect(phone.press / 0.0044).toBeCloseTo(44, 6)
    expect(phone.draw / 0.0044).toBeCloseTo(20, 6)
    // The glasses: one pixel of the display covers about a millimetre of a panel at 1.75 m. The edge decides.
    const glasses = closeSize(0.052, 0.00104)
    expect(glasses.press).toBeCloseTo(0.104, 6)
    expect(glasses.draw).toBeCloseTo(0.0234, 6)
    expect(glasses.press / 0.00104).toBeGreaterThan(CLOSE_MARK.pressPx)
    expect(glasses.draw / 0.00104).toBeGreaterThan(CLOSE_MARK.drawPx)
  })

  it('stays lit for a finger long enough to be seen and tapped', () => {
    expect(CLOSE_MARK.lingerMs).toBeGreaterThanOrEqual(2000)
    expect(CLOSE_MARK.lingerMs).toBeGreaterThan(CLOSE_MARK.shownMs)
  })
})
