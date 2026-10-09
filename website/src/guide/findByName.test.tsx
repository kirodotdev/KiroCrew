/**
 * `ui.find`: finding a control by its accessible name, refusing what belongs
 * to the agent's own ceiling, and probing only the containers a shared
 * primitive registered, through their own state, without leaving anything
 * changed.
 *
 * happy-dom lays nothing out, so every element is given a box here; what is
 * hidden is hidden the way the page hides it (not rendered, or `hidden`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { useState } from 'react'
import {
  abortProbes, accessibleName, distinguishingContexts, findCandidates, findNeedsPick, findReport, findState, normalizeName, probeForName,
  resetFindStates, resolveContainerPath, resolveFind, searchByName, setFindPick, setFindState,
} from './findByName'
import { isCautionTarget, isTrustRootTarget } from './findTargetPolicy'
import { probeTargets, registerProbeTarget } from './probeRegistry'
import { guideCaution, guideTrustRoot, GuideTrustRootProvider } from './trustRoot'
import TrustDropdown from '../components/TrustDropdown'
import Modal from '../components/Modal'
import { SettingsSubNav } from '../components/SettingsSubNav'
import { resolveGuideAction } from './guideActions'
import { useGuideStepTracker, GUIDE_EARLIER_STEP_WAIT_MS, GUIDE_FOUND_RETRY_MS } from './useGuideStepTracker'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { Popover, PopoverContent, PopoverTrigger } from '../components/ui/popover'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { i18next } from '../i18n/all'
import { MemoryRouter } from 'react-router-dom'
import { GUIDE_FIND_SETTLE_MS, useFindProbe } from './useFindProbe'
import { marks } from '../test/guideTargets'

const realRect = HTMLElement.prototype.getBoundingClientRect
beforeEach(() => {
  HTMLElement.prototype.getBoundingClientRect = function () {
    return { top: 10, left: 10, width: 40, height: 20, right: 50, bottom: 30, x: 10, y: 10, toJSON: () => ({}) } as DOMRect
  }
})
afterEach(() => {
  cleanup()
  HTMLElement.prototype.getBoundingClientRect = realRect
  resetFindStates()
  window.history.replaceState(null, '', '/')
})

describe('searchByName', () => {
  it('finds an exact name, and says the name the page uses', () => {
    render(<><button>Run Now</button><button>Run Now later</button></>)
    const r = searchByName({ label: 'Run Now' })
    expect(r.result).toBe('found')
    expect(accessibleName(r.element!)).toBe('Run Now')
  })

  it('folds case, spacing and punctuation when nothing matches exactly', () => {
    render(<button aria-label="Run now…">▶</button>)
    const r = searchByName({ label: 'run  NOW' })
    expect(r.result).toBe('found')
    expect(accessibleName(r.element!)).toBe('Run now…')
    expect(normalizeName('Ｒｕｎ－Ｎｏｗ')).toBe(normalizeName('run now'))
  })

  it('never takes a name that only contains the label', () => {
    // "Remove" is not "Report a problem — secrets removed"; "Add…" is not "Add files & options".
    render(<><button>Show all older sessions</button><button>Report a problem — secrets removed</button><button aria-label="Add files &amp; options">+</button></>)
    expect(searchByName({ label: 'older sessions' }).result).toBe('none')
    expect(searchByName({ label: 'Remove' }).result).toBe('none')
    expect(searchByName({ label: 'Add…' }).result).toBe('none')
  })

  it('never takes a control registered as another location for a registered one', () => {
    render(<><button {...marks({ location: "composer.add-menu" })}>Add</button><button data-testid="plain">Add</button></>)
    expect(searchByName({ label: 'Add', location: 'members.add-menu' }).element?.dataset.testid).toBe('plain')
    render(<button {...marks({ location: "composer.attach" })}>Attach</button>)
    expect(searchByName({ label: 'Attach', location: 'members.attach' }).result).toBe('none')
    expect(searchByName({ label: 'Attach' }).result).toBe('found')
  })

  it('prefers a control over a section title of the same name, and a nav entry for a settings sub-page', () => {
    render(
      <>
        <nav aria-label="Display"><button data-testid="nav">Theme</button></nav>
        <section><h3><button data-testid="title">Theme</button></h3><label htmlFor="th">Theme</label><select id="th" data-testid="select"><option>Kiro</option></select></section>
      </>,
    )
    // The heading's button gives way; the nav entry and the field remain.
    expect(searchByName({ label: 'Theme' }).matches.map(e => e.dataset.testid)).toEqual(['nav', 'select'])
    expect(searchByName({ label: 'Theme', role: 'tab' }).element?.dataset.testid).toBe('nav')
    expect(searchByName({ label: 'Theme', location: 'settings.sub.display.theme' }).element?.dataset.testid).toBe('nav')
  })

  it('narrows by role, and drops a role that matches nothing', () => {
    render(<><a href="/x">Export</a><button>Export</button></>)
    expect(searchByName({ label: 'Export' }).result).toBe('ambiguous')
    const r = searchByName({ label: 'Export', role: 'link' })
    expect(r.result).toBe('found')
    expect(r.element!.tagName).toBe('A')
    expect(searchByName({ label: 'Export', role: 'tab' }).result).toBe('ambiguous')
  })

  it('finds the real dashboard shapes whose role differs from the index\'s kind', () => {
    render(
      <>
        {/* A Settings sub-page row: indexed as a tab, drawn as a list option. */}
        <div role="listbox"><button role="option" aria-selected="false" {...marks({ target: "settings.sub.channels.slack" })}>Slack<span>Needs setup</span></button></div>
        {/* A side-panel tab: indexed as a tab, drawn as a plain button. */}
        <nav><button>About</button></nav>
        {/* A panel toggle: indexed as a toggle, drawn as a pressed button. */}
        <button aria-pressed="false" aria-label="Dashboard &amp; files">▤</button>
        {/* A Connections tab: indexed as a button, drawn as a tab. */}
        <div role="tablist"><button role="tab" aria-selected="false">Services</button></div>
      </>,
    )
    // Its name carries its status line: only its registered id finds it.
    expect(searchByName({ label: 'Slack', role: 'tab' }).result).toBe('none')
    expect(searchByName({ label: 'Slack', role: 'tab', location: 'settings.sub.channels.slack' }).result).toBe('found')
    expect(searchByName({ label: 'About', role: 'tab' }).result).toBe('found')
    expect(searchByName({ label: 'Dashboard & files', role: 'switch' }).result).toBe('found')
    expect(searchByName({ label: 'Services', role: 'button' }).result).toBe('found')
  })

  it('never matches a visually hidden 1px control', () => {
    render(<input type="file" aria-label="Attach files" data-testid="file" />)
    const file = document.querySelector<HTMLElement>('[data-testid="file"]')!
    file.getBoundingClientRect = () => ({ top: 0, left: 0, width: 1, height: 1, right: 1, bottom: 1, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
    expect(searchByName({ label: 'Attach files', role: 'button' }).result).toBe('none')
  })

  it('treats a switch and a checkbox alike, and a menu item of any kind as a menu item', () => {
    render(<><button role="switch" aria-checked="false">Dark mode</button><div role="menuitemcheckbox" aria-checked="false">Pin</div></>)
    expect(searchByName({ label: 'Dark mode', role: 'checkbox' }).result).toBe('found')
    expect(searchByName({ label: 'Pin', role: 'menuitem' }).result).toBe('found')
  })

  it('names a form field by its label or placeholder', () => {
    // The second field is named by its placeholder alone, on purpose.
    // eslint-disable-next-line jsx-a11y/control-has-associated-label
    render(<><label htmlFor="q">Search jobs</label><input id="q" /><input placeholder="Filter" /></>)
    expect(searchByName({ label: 'Search jobs', role: 'textbox' }).result).toBe('found')
    expect(searchByName({ label: 'Filter', role: 'textbox' }).result).toBe('found')
  })

  it('names matches of different kinds by region and kind, never by a landmark alone', () => {
    render(
      <>
        <nav aria-label="Main navigation"><a href="/members">Crewmates</a></nav>
        <div role="tablist" aria-label="Customize"><button role="tab">Crewmates</button></div>
      </>,
    )
    const r = searchByName({ label: 'Crewmates' })
    expect(r.result).toBe('ambiguous')
    expect(distinguishingContexts(r.matches)).toEqual(['Crewmates · link · Main navigation', 'Crewmates · tab · Customize'])
  })

  it('prefers the page tab over the main-menu entry of the same name for a tab target', () => {
    render(
      <>
        <nav aria-label="Main navigation"><a href="/members">Crewmates</a></nav>
        <nav aria-label="Customize"><button role="tab">Crewmates</button></nav>
      </>,
    )
    const r = searchByName({ label: 'Crewmates', location: 'tab.capabilities.crews' })
    expect(r.result).toBe('found')
    expect(r.element?.getAttribute('role')).toBe('tab')
    // A search that is not for a tab still sees both.
    expect(searchByName({ label: 'Crewmates' }).result).toBe('ambiguous')
  })

  it('never offers a link inside a chat reply as a candidate', () => {
    render(
      <>
        <nav aria-label="Main navigation"><a href="/artifacts">Artifacts</a></nav>
        <div className="message-bubble"><p>They are under <a href="/artifacts">Artifacts</a>.</p></div>
      </>,
    )
    const r = searchByName({ label: 'Artifacts' })
    expect(r.result).toBe('found')
    expect(r.element?.closest('.message-bubble')).toBeNull()
  })

  it('reports several matches as ambiguous, with what surrounds each, and narrows by the container hint', () => {
    render(
      <>
        <section aria-label="Daily digest"><button>Run Now</button></section>
        <section aria-label="Weekly report"><button>Run Now</button></section>
      </>,
    )
    const r = searchByName({ label: 'Run Now' })
    expect(r.result).toBe('ambiguous')
    expect(distinguishingContexts(r.matches)).toEqual(['Daily digest', 'Weekly report'])
    const narrowed = searchByName({ label: 'Run Now', container: 'weekly' })
    expect(narrowed.result).toBe('found')
  })

  it("names a control drawn on every row by its own name and the row's item, never by the row's own action label", () => {
    render(
      <>
        <div role="button" tabIndex={0} aria-label="View details for Command Bar" {...marks({ pick: "Command Bar" })}><span>Command Bar</span><button>Install</button></div>
        <div role="button" tabIndex={0} aria-label="View details for Notes" {...marks({ pick: "Notes" })}><span>Notes</span><button>Install</button></div>
      </>,
    )
    const r = searchByName({ label: 'Install' })
    expect(r.result).toBe('ambiguous')
    expect(distinguishingContexts(r.matches)).toEqual(['Install · Command Bar', 'Install · Notes'])
    cleanup()
    // Without item names, a surrounding control's label is still never the context.
    render(
      <>
        <section aria-label="Store"><div role="button" tabIndex={0} aria-label="View details for A"><button>Get</button></div></section>
        <section aria-label="Library"><div role="button" tabIndex={0} aria-label="View details for B"><button>Get</button></div></section>
      </>,
    )
    const g = searchByName({ label: 'Get' })
    expect(distinguishingContexts(g.matches)).toEqual(['Store', 'Library'])
  })

  it('never takes a numbered container hint as a candidate: the person picks in the panel', () => {
    render(<section aria-label="Jobs"><button data-testid="a">Run</button><button data-testid="b">Run</button></section>)
    // Never a bare number: the panel numbers its list itself.
    expect(distinguishingContexts(searchByName({ label: 'Run' }).matches)).toEqual(['Jobs', 'Jobs'])
    // A number names a match only in a list the person saw; it never narrows.
    for (const container of ['#2', '1', '#3']) {
      const r = searchByName({ label: 'Run', container })
      expect(r.result).toBe('ambiguous')
      expect(r.matches).toHaveLength(2)
    }
  })

  it('a numbered hint never binds to whatever is Nth after the list reorders', () => {
    const Rows = ({ order }: { order: string[] }) => <>{order.map(n => <section key={n} aria-label={n}><button data-testid={n}>Run</button></section>)}</>
    const { rerender } = render(<Rows order={['Daily', 'Weekly']} />)
    const q = { label: 'Run', container: '#2' }
    expect(resolveFind(q, 'kn')).toBeNull()
    rerender(<Rows order={['Weekly', 'Daily']} />)
    expect(resolveFind(q, 'kn')).toBeNull()
    expect(findReport(q, 'kn')).toEqual({ result: 'ambiguous', count: 2 })
  })

  it('a remounted list is never re-bound to the pick by position and text: the person is asked again', () => {
    // Same names, same contexts, same order, but new elements (another entity
    // can now sit in the picked row's place).
    const Rows = ({ gen }: { gen: number }) => <>{['A', 'B'].map(n => <section key={`${gen}-${n}`} aria-label="Tasks"><h3>{n}</h3><button data-testid={`${gen}-${n}`}>Run</button></section>)}</>
    const { rerender } = render(<Rows gen={1} />)
    const q = { label: 'Run' }
    findCandidates(q, 'kr')
    setFindPick('kr', 2)
    expect(resolveFind(q, 'kr')?.dataset.testid).toBe('1-B')
    rerender(<Rows gen={2} />)
    expect(resolveFind(q, 'kr')).toBeNull()
    expect(findNeedsPick(q, 'kr')).toBe(true)
  })

  it('tells matches apart by the section title above each when their surroundings read the same', () => {
    // Both rows read "Tasks" around them; only the heading before each differs.
    const Rows = ({ order }: { order: string[] }) => <main>{order.map(n => <div key={n}><h2>{n}</h2><div><button data-testid={n}>Run</button></div></div>)}</main>
    const { rerender } = render(<Rows order={['Alpha', 'Beta']} />)
    const q = { label: 'Run' }
    findCandidates(q, 'kh')
    setFindPick('kh', 1)
    expect(resolveFind(q, 'kh')?.dataset.testid).toBe('Alpha')
    // React reuses the keyed elements when reordering; the set read by
    // heading is a different list now, so the pick is stale.
    rerender(<Rows order={['Beta', 'Alpha']} />)
    expect(resolveFind(q, 'kh')).toBeNull()
  })

  it('names a candidate with no labelled surroundings by the section title above it', () => {
    render(
      <main>
        <h2>Daily digest</h2><div><button>Run</button></div>
        <h2>Weekly report</h2><div><button>Run</button></div>
      </main>,
    )
    expect(distinguishingContexts(searchByName({ label: 'Run' }).matches)).toEqual(['Daily digest', 'Weekly report'])
  })

  it('never matches the guide panel itself, a hidden control, or nothing', () => {
    render(
      <>
        <div data-testid="guide-pill"><button>Run Now</button></div>
        <div hidden><button>Run Now</button></div>
      </>,
    )
    expect(searchByName({ label: 'Run Now' }).result).toBe('none')
    expect(searchByName({ label: '  ' }).result).toBe('none')
  })
})

// ── H1: the agent's own ceiling, judged on what matched ──

describe('a match of the agent\'s own ceiling', () => {
  it('is refused on a trust-root page even when the guide named no route', () => {
    window.history.replaceState(null, '', '/settings/security')
    render(<button>Profiles</button>)
    expect(searchByName({ label: 'Profiles' }).result).toBe('sensitive')
    expect(resolveFind({ label: 'Profiles' }, 'k')).toBeNull()
    expect(findReport({ label: 'Profiles' }, 'k')).toEqual({ result: 'none', count: 0 })
  })

  it('is refused inside a region that marks itself trust-root, on any page', () => {
    window.history.replaceState(null, '', '/chat/slot-A')
    render(<><div {...guideTrustRoot}><button>Run anyway</button></div><button>Copy</button></>)
    expect(searchByName({ label: 'Run anyway' }).result).toBe('sensitive')
    expect(searchByName({ label: 'Copy' }).result).toBe('found')
  })

  it('is refused when the label only CONTAINS part of a ceiling control\'s name', () => {
    // The gateway's ceiling rule sees "Auto mode", which names nothing it refuses.
    render(<button>Autopilot mode</button>)
    expect(searchByName({ label: 'pilot mode' }).result).toBe('sensitive')
  })

  it('is refused by the matched control\'s identity: a ceiling tab, a sensitive setting row, a ceiling key', async () => {
    const entry = SETTINGS_REGISTRY.find(e => e.id === 'skills.require-approval-before-generated-skills-go-live')!
    render(
      <>
        <button {...marks({ location: "settings.tab.security" })}>Shield</button>
        <div data-setting-key={entry.configKey}><button role="switch" aria-checked="true">Gatekeep skills</button></div>
        <button {...marks({ auto: "auto:settings.tab.secrets:SecretsPanel:settings.secrets.add" })}>Add one</button>
      </>,
    )
    expect(searchByName({ label: 'Shield' }).result).toBe('sensitive')
    expect(searchByName({ label: 'Gatekeep skills' }).result).toBe('sensitive')
    expect(searchByName({ label: 'Add one' }).result).toBe('sensitive')
    // A Chinese "批准" is refused by the catalog key its name renders from.
    await i18next.changeLanguage('zh-CN')
    try {
      cleanup()
      render(<button>{i18next.t('components.approvalCard.approve')}</button>)
      const el = document.querySelector('button')!
      expect(isTrustRootTarget(el, accessibleName(el))).toBe(true)
    } finally {
      await i18next.changeLanguage('en')
    }
  })
})

// ── L6: caution from the matched control's identity ──

describe('caution', () => {
  it('follows the control, not the language the agent asked in', async () => {
    render(<><button {...marks({ location: "agents.delete" })}>Bye</button><button {...marks({ auto: "auto:jobs:JobRow:pages.schedulePage.delete" })}>删除</button><button>Rename</button></>)
    const [curated, auto, plain] = Array.from(document.querySelectorAll('button'))
    expect(isCautionTarget(curated, 'Bye')).toBe(true)
    expect(isCautionTarget(auto, '删除')).toBe(true)
    expect(isCautionTarget(plain, 'Rename')).toBe(false)
    await i18next.changeLanguage('zh-CN')
    try {
      cleanup()
      render(<button>{i18next.t('apps.mochi.gallery.delete')}</button>)
      const el = document.querySelector('button')!
      expect(accessibleName(el)).toBe('删除')
      expect(isCautionTarget(el, accessibleName(el))).toBe(true)
    } finally {
      await i18next.changeLanguage('en')
    }
  })

  it('marks a destructive control whose name is built at run time, in any language', async () => {
    await i18next.changeLanguage('zh-CN')
    try {
      const name = i18next.t('apps.papyrus.page.delete_paper', { name: 'thesis' })
      expect(name).toBe('删除 thesis')
      // The explicit, language-independent marker, on a name no catalog value renders.
      render(<><button aria-label="thesis" {...guideCaution}>×</button><button aria-label="thesis">×</button></>)
      const [marked, plain] = Array.from(document.querySelectorAll('button'))
      expect(isCautionTarget(marked, accessibleName(marked))).toBe(true)
      expect(isCautionTarget(plain, accessibleName(plain))).toBe(false)
      // And without it, the interpolated catalog value still maps back to its key.
      cleanup()
      render(<button aria-label={name}>×</button>)
      const bare = document.querySelector('button')!
      expect(isCautionTarget(bare, accessibleName(bare))).toBe(true)
    } finally {
      await i18next.changeLanguage('en')
    }
  })
})

// ── trust roots across portals and interpolated names ──

describe('trust roots', () => {
  it('refuses a control a trust-root region renders through a portal', () => {
    render(
      <GuideTrustRootProvider>
        <DropdownMenu modal={false} defaultOpen>
          <DropdownMenuTrigger asChild><button>Options</button></DropdownMenuTrigger>
          <DropdownMenuContent><DropdownMenuItem>Export log</DropdownMenuItem></DropdownMenuContent>
        </DropdownMenu>
      </GuideTrustRootProvider>,
    )
    const item = Array.from(document.querySelectorAll('[role="menuitem"]')).find(e => e.textContent === 'Export log')!
    // Portalled out of the region: the marker sits on the menu itself.
    expect(item.closest('[role="menu"]')?.hasAttribute('data-guide-trust-root')).toBe(true)
    expect(searchByName({ label: 'Export log' }).result).toBe('sensitive')
  })

  it('refuses a control in a modal a trust-root region opens', () => {
    render(<GuideTrustRootProvider><Modal open onClose={() => {}} title="Confirm"><button>Turn it off</button></Modal></GuideTrustRootProvider>)
    expect(searchByName({ label: 'Turn it off' }).result).toBe('sensitive')
    cleanup()
    render(<Modal open onClose={() => {}} title="Confirm"><button>Turn it off</button></Modal>)
    expect(searchByName({ label: 'Turn it off' }).result).toBe('found')
  })

  it('refuses a Chinese trust item of the approval menu found inside its portal', async () => {
    await i18next.changeLanguage('zh-CN')
    try {
      render(<TrustDropdown fullCommand="npm test" baseCommand="npm" isShell hasCommand onAction={() => {}} />)
      fireEvent.pointerDown(document.querySelector('button')!, { button: 0, ctrlKey: false })
      const items = Array.from(document.querySelectorAll('[role="menuitem"]'))
      expect(items.length).toBeGreaterThan(0)
      expect(searchByName({ label: 'npm test' }).result).toBe('sensitive')
      expect(findReport({ label: 'npm test' }, 'k')).toEqual({ result: 'none', count: 0 })
    } finally {
      await i18next.changeLanguage('en')
    }
  })

  it('refuses a Chinese trust grant by the interpolated catalog value it renders from, marked or not', async () => {
    await i18next.changeLanguage('zh-CN')
    try {
      render(<button>信任“<span>npm test</span>”</button>)
      const el = document.querySelector('button')!
      expect(isTrustRootTarget(el, accessibleName(el))).toBe(true)
      cleanup()
      render(<button>npm test</button>)
      const plain = document.querySelector('button')!
      expect(isTrustRootTarget(plain, accessibleName(plain))).toBe(false)
    } finally {
      await i18next.changeLanguage('en')
    }
  })
})

// ── the registered containers a probe may open ──

/** Radix menus, popovers and tabs as the dashboard uses them. */
function Menu({ label, children, trigger, ...props }: { label: string; children: React.ReactNode; onOpenChange?: (o: boolean) => void; guideProbe?: boolean; trigger?: Record<string, string> }) {
  return (
    <DropdownMenu modal={false} {...props}>
      <DropdownMenuTrigger asChild><button {...trigger}>{label}</button></DropdownMenuTrigger>
      <DropdownMenuContent>{children}</DropdownMenuContent>
    </DropdownMenu>
  )
}

function LocalTabs({ panes, local = true, onValueChange }: { panes: Record<string, React.ReactNode>; local?: boolean; onValueChange?: (v: string) => void }) {
  const names = Object.keys(panes)
  return (
    <Tabs defaultValue={names[0]} {...(local ? { guideProbe: 'local' as const } : {})} {...(onValueChange ? { onValueChange } : {})}>
      <TabsList>{names.map(n => <TabsTrigger key={n} value={n}>{n}</TabsTrigger>)}</TabsList>
      {names.map(n => <TabsContent key={n} value={n}>{panes[n]}</TabsContent>)}
    </Tabs>
  )
}

const selectedTab = () => document.querySelector('[role="tab"][aria-selected="true"]')?.textContent

/**
 * Run a probe the way a page does: outside `act`, which would hold every
 * render back until it ends, so an open would never show what it revealed.
 */
const probe = async (q: Parameters<typeof probeForName>[0], opts: Parameters<typeof probeForName>[1] = {}) => {
  const g = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  const prior = g.IS_REACT_ACT_ENVIRONMENT
  g.IS_REACT_ACT_ENVIRONMENT = false
  try {
    return await probeForName(q, { settleMs: 5, ...opts })
  } finally {
    g.IS_REACT_ACT_ENVIRONMENT = prior
  }
}

/** Every event the probe could have dispatched, heard at the window. */
function listenForEvents() {
  const heard: string[] = []
  const types = ['pointerdown', 'mousedown', 'click', 'keydown'] as const
  const on = (e: Event) => { heard.push(e instanceof KeyboardEvent ? `${e.type}:${e.key}` : e.type) }
  for (const t of types) window.addEventListener(t, on, true)
  return { heard, stop: () => { for (const t of types) window.removeEventListener(t, on, true) } }
}

describe('probeForName', () => {
  it('opens a registered menu through its own state, then closes it, moving no focus and dispatching no event', async () => {
    render(<><button data-testid="draft">Draft</button><Menu label="More actions"><DropdownMenuItem>Export</DropdownMenuItem></Menu></>)
    const draft = document.querySelector<HTMLButtonElement>('[data-testid="draft"]')!
    draft.focus()
    const moved: string[] = []
    const onFocus = (e: FocusEvent) => { moved.push(`${e.type}:${(e.target as Element)?.tagName}`) }
    document.addEventListener('focusin', onFocus, true)
    document.addEventListener('focusout', onFocus, true)
    const events = listenForEvents()
    const r = await probe({ label: 'Export', role: 'menuitem' })
    events.stop()
    expect(r).toEqual({ result: 'found', path: [{ id: expect.any(Number), name: 'More actions', role: 'button', kind: 'popup' }] })
    expect(document.querySelector('[role="menu"]')).toBeNull()
    document.removeEventListener('focusin', onFocus, true)
    document.removeEventListener('focusout', onFocus, true)
    expect(document.activeElement).toBe(draft)
    // Focus never left the person's control: no blur, so nothing a blur saves fires.
    expect(moved).toEqual([])
    // No press, no Escape: nothing the page's own handlers could take for a person.
    expect(events.heard).toEqual([])
  })

  it('opens nothing while the person is typing in a field, and fires none of its blur handlers', async () => {
    const onBlur = vi.fn()
    render(<><input data-testid="draft" aria-label="Draft" onBlur={onBlur} /><Menu label="More actions"><DropdownMenuItem>Export</DropdownMenuItem></Menu></>)
    const draft = document.querySelector<HTMLInputElement>('[data-testid="draft"]')!
    draft.focus()
    const opened = vi.fn()
    const off = registerProbeTarget({ kind: 'popup', trigger: () => draft, isOpen: () => false, open: () => { opened(); return () => {} } })
    try {
      expect(await probe({ label: 'Export', role: 'menuitem' })).toEqual({ result: 'none' })
    } finally { off() }
    expect(opened).not.toHaveBeenCalled()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(draft)
    expect(onBlur).not.toHaveBeenCalled()
  })

  it('does not start once the person acted since the step began', async () => {
    render(<button data-testid="box">Box</button>)
    const open = vi.fn(() => () => {})
    const off = registerProbeTarget({ kind: 'popup', trigger: () => document.querySelector('[data-testid="box"]'), isOpen: () => false, open })
    try {
      expect(await probe({ label: 'Export' }, { interrupted: () => true })).toEqual({ result: 'none' })
    } finally { off() }
    expect(open).not.toHaveBeenCalled()
  })

  it('opens a popover without its autofocus taking focus from the person', async () => {
    render(
      <>
        <button data-testid="mine">Mine</button>
        <Popover>
          <PopoverTrigger asChild><button>Filters</button></PopoverTrigger>
          <PopoverContent><input aria-label="Query" /><button>Apply filter</button></PopoverContent>
        </Popover>
      </>,
    )
    const mine = document.querySelector<HTMLButtonElement>('[data-testid="mine"]')!
    mine.focus()
    const moved: string[] = []
    const onFocus = (e: FocusEvent) => { moved.push(e.type) }
    document.addEventListener('focusin', onFocus, true)
    document.addEventListener('focusout', onFocus, true)
    try {
      expect((await probe({ label: 'Apply filter' })).result).toBe('found')
    } finally {
      document.removeEventListener('focusin', onFocus, true)
      document.removeEventListener('focusout', onFocus, true)
    }
    expect(document.activeElement).toBe(mine)
    expect(moved).toEqual([])
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })

  it('keeps what it opened from being dismissed by a focus change outside it, until the person acts', async () => {
    render(
      <>
        <button data-testid="elsewhere">Elsewhere</button>
        <Popover>
          <PopoverTrigger asChild><button>Filters</button></PopoverTrigger>
          <PopoverContent><button>Apply filter</button></PopoverContent>
        </Popover>
      </>,
    )
    const running = probe({ label: 'Apply filter' }, { settleMs: 120 })
    await waitFor(() => expect(document.querySelector('[role="dialog"]')).not.toBeNull())
    // Focus moved by the page's own code, not by the person: no dismissal.
    document.querySelector<HTMLButtonElement>('[data-testid="elsewhere"]')!.focus()
    expect((await running).result).toBe('found')
  })

  it('stops at once when the guide is cancelled, restoring what it opened', async () => {
    render(<>{[0, 1, 2].map(i => <button key={i} data-testid={`box-${i}`}>{`Box ${i}`}</button>)}</>)
    const opened: number[] = []
    const restored: number[] = []
    const offs = [0, 1, 2].map((i) => {
      let open = false
      return registerProbeTarget({
        kind: 'popup',
        trigger: () => document.querySelector(`[data-testid="box-${i}"]`),
        isOpen: () => open,
        open: () => { open = true; opened.push(i); return () => { open = false; restored.push(i) } },
      })
    })
    try {
      const running = probe({ label: 'Nowhere' }, { settleMs: 100 })
      await waitFor(() => expect(opened).toEqual([0]))
      expect(opened).toEqual([0])
      abortProbes()
      expect(await running).toEqual({ result: 'none' })
      // The first was put back; the others were never opened.
      expect(opened).toEqual([0])
      expect(restored).toEqual([0])
    } finally { for (const off of offs) off() }
  })

  it('selects a local tab to look, then selects the original back', async () => {
    render(<LocalTabs panes={{ General: <button>Save</button>, Advanced: <button>Purge cache</button> }} />)
    const r = await probe({ label: 'Purge cache' })
    expect(r).toEqual({ result: 'found', path: [{ id: expect.any(Number), name: 'Advanced', role: 'tab', kind: 'tab' }] })
    expect(selectedTab()).toBe('General')
  })

  it('looks one registered container deeper, and restores both', async () => {
    render(
      <Popover>
        <PopoverTrigger asChild><button>Filters</button></PopoverTrigger>
        <PopoverContent><LocalTabs panes={{ Basic: <button>Apply</button>, More: <button>Rebuild index</button> }} /></PopoverContent>
      </Popover>,
    )
    const r = await probe({ label: 'Rebuild index' })
    expect(r.result).toBe('found')
    expect(r.result === 'found' && r.path.map(p => [p.name, p.kind])).toEqual([['Filters', 'popup'], ['More', 'tab']])
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect((await probe({ label: 'Rebuild index' }, { maxDepth: 1 })).result).toBe('none')
  })

  it('never opens a tab rail that is not declared local (one that navigates)', async () => {
    const navigate = vi.fn()
    render(<LocalTabs local={false} onValueChange={navigate} panes={{ Discover: <button>Search</button>, Installed: <button>Update all</button> }} />)
    expect((await probe({ label: 'Update all' })).result).toBe('none')
    expect(navigate).not.toHaveBeenCalled()
    expect(selectedTab()).toBe('Discover')
  })

  it('never opens a menu whose opening something outside it hears, or one that opted out', async () => {
    const heard = vi.fn()
    render(
      <>
        <Menu label="Wired" onOpenChange={heard}><DropdownMenuItem>Export</DropdownMenuItem></Menu>
        <Menu label="Opted out" guideProbe={false}><DropdownMenuItem>Import</DropdownMenuItem></Menu>
      </>,
    )
    expect((await probe({ label: 'Export' })).result).toBe('none')
    expect((await probe({ label: 'Import' })).result).toBe('none')
    expect(heard).not.toHaveBeenCalled()
  })

  it('never presses an unregistered container: a destructive confirm toggle, a Sketch-style dialog button, a hand-rolled menu, a <details>', async () => {
    const pressed = vi.fn()
    function DeleteToggle() {
      // The workspace row's armed delete: a plain button with aria-expanded.
      const [armed, setArmed] = useState(false)
      return <><button aria-expanded={armed} onClick={() => { pressed(); setArmed(true) }}>Delete</button>{armed && <input aria-label="Type the workspace name to confirm" />}</>
    }
    render(
      <>
        <DeleteToggle />
        <button aria-haspopup="dialog" onClick={pressed} onPointerDown={pressed}>Sketch</button>
        <button aria-haspopup="menu" aria-expanded="false" onClick={pressed}>More</button>
        <details><summary onClick={pressed}>Advanced</summary><button>Hidden</button></details>
      </>,
    )
    const events = listenForEvents()
    expect((await probe({ label: 'Type the workspace name to confirm' })).result).toBe('none')
    expect((await probe({ label: 'Hidden' })).result).toBe('none')
    events.stop()
    expect(pressed).not.toHaveBeenCalled()
    expect(events.heard).toEqual([])
    expect(probeTargets()).toEqual([])
  })

  it('stops when its signal aborts (the step changed), and restores what it opened', async () => {
    const selected = vi.fn()
    render(<LocalTabs onValueChange={selected} panes={{ General: <button>Save</button>, Advanced: <button>Nothing here</button>, Third: <button>Purge cache</button> }} />)
    const controller = new AbortController()
    const run = probe({ label: 'Purge cache' }, { signal: controller.signal, settleMs: 20 })
    controller.abort()
    expect(await run).toEqual({ result: 'none' })
    expect(selectedTab()).toBe('General')
    // Advanced was opened and selected back; Third never.
    expect(selected.mock.calls.map(c => c[0])).toEqual(['Advanced', 'General'])
    expect((await probe({ label: 'Save' }, { signal: controller.signal })).result).toBe('none')
  })

  it('stops at real input outside the guide, and does not restore over what the person now sees', async () => {
    render(<><Menu label="More"><DropdownMenuItem>Nothing here</DropdownMenuItem></Menu><LocalTabs panes={{ General: <button>Save</button>, Advanced: <button>Purge cache</button> }} /></>)
    const run = probe({ label: 'Purge cache' }, { settleMs: 20 })
    // The probe holds the menu open; the person types somewhere on the page.
    fireEvent.keyDown(document.body, { key: 'a' })
    expect(await run).toEqual({ result: 'none' })
    await act(async () => {})
    // Not closed over the person's input, and nothing further opened.
    expect(document.querySelector('[role="menu"]')).not.toBeNull()
    expect(selectedTab()).toBe('General')
  })

  it('ignores input inside the guide\'s own panel', async () => {
    render(<><div data-testid="guide-pill"><button>Cancel guide</button></div><Menu label="More"><DropdownMenuItem>Export</DropdownMenuItem></Menu></>)
    const run = probe({ label: 'Export' }, { settleMs: 20 })
    fireEvent.pointerDown(document.querySelector('[data-testid="guide-pill"] button')!)
    expect((await run).result).toBe('found')
  })

  it('opens at most maxContainers containers and stops at its time budget', async () => {
    const opened = vi.fn()
    render(<>{Array.from({ length: 20 }, (_, i) => <button key={i} data-testid={`t${i}`}>{`Section ${i}`}</button>)}</>)
    const offs = Array.from({ length: 20 }, (_, i) => registerProbeTarget({
      kind: 'popup',
      trigger: () => document.querySelector<HTMLElement>(`[data-testid="t${i}"]`),
      isOpen: () => false,
      open: () => { opened(); return () => {} },
    }))
    try {
      await probe({ label: 'Nowhere' }, { settleMs: 0 })
      expect(opened).toHaveBeenCalledTimes(12)
      opened.mockClear()
      let t = 0
      await probe({ label: 'Nowhere' }, { settleMs: 0, now: () => (t += 1000), budgetMs: 1500 })
      expect(opened.mock.calls.length).toBeLessThanOrEqual(1)
    } finally { offs.forEach(off => off()) }
  })

  it('stops and restores when opening a container changed the address', async () => {
    const restored = vi.fn()
    const opened = vi.fn()
    render(<><button data-testid="nav">Go</button><button data-testid="later">Later</button></>)
    const offs = [
      registerProbeTarget({ kind: 'popup', trigger: () => document.querySelector('[data-testid="nav"]'), isOpen: () => false, open: () => { window.history.pushState(null, '', '/elsewhere'); return restored } }),
      registerProbeTarget({ kind: 'popup', trigger: () => document.querySelector('[data-testid="later"]'), isOpen: () => false, open: () => { opened(); return () => {} } }),
    ]
    try {
      expect((await probe({ label: 'Target' }, { settleMs: 0 })).result).toBe('none')
      expect(restored).toHaveBeenCalledTimes(1)
      expect(opened).not.toHaveBeenCalled()
    } finally { offs.forEach(off => off()) }
  })

  it('tries the container whose name is nearest the hint first', async () => {
    const order: string[] = []
    render(<><button data-testid="a">Appearance</button><button data-testid="n">Notifications settings</button></>)
    const offs = ['a', 'n'].map(id => registerProbeTarget({
      kind: 'popup',
      trigger: () => document.querySelector(`[data-testid="${id}"]`),
      isOpen: () => false,
      open: () => { order.push(id); return () => {} },
    }))
    try {
      await probe({ label: 'Nowhere', container: 'notifications' }, { settleMs: 0 })
      expect(order[0]).toBe('n')
    } finally { offs.forEach(off => off()) }
  })

  it('keeps the container of several matches, and its count, so the person can pick there', async () => {
    render(<Menu label="More"><DropdownMenuItem>Copy</DropdownMenuItem><DropdownMenuItem>Copy</DropdownMenuItem></Menu>)
    expect(await probe({ label: 'Copy' })).toEqual({ result: 'ambiguous', path: [{ id: expect.any(Number), name: 'More', role: 'button', kind: 'popup' }], count: 2 })
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })

  it('never probes a trust-root page, or a container that is trust-root itself', async () => {
    // The approval-mode picker's own shape: the marker on the trigger, the provider around the menu.
    render(<GuideTrustRootProvider><Menu label="Approval mode" trigger={guideTrustRoot}><DropdownMenuItem>YOLO</DropdownMenuItem></Menu></GuideTrustRootProvider>)
    expect((await probe({ label: 'Edit' })).result).toBe('none')
    expect(document.querySelector('[role="menu"]')).toBeNull()
    cleanup()
    window.history.replaceState(null, '', '/settings/security')
    render(<Menu label="Profiles"><DropdownMenuItem>Edit</DropdownMenuItem></Menu>)
    expect((await probe({ label: 'Edit' })).result).toBe('none')
  })
})

describe('useFindProbe', () => {
  it('opens nothing when the person acted after the step began, during the settle wait', async () => {
    const q = { label: 'Deep' }
    const key = 'acted-key'
    const open = vi.fn(() => () => {})
    function Host() {
      useFindProbe({ runId: 'r', query: q, findKey: key, enabled: true })
      return <button data-testid="box">Box</button>
    }
    render(<MemoryRouter><Host /></MemoryRouter>)
    const off = registerProbeTarget({ kind: 'popup', trigger: () => document.querySelector('[data-testid="box"]'), isOpen: () => false, open })
    try {
      // Before the probe starts: heard because the listener is the step's.
      fireEvent.keyDown(document.body, { key: 'a' })
      await waitFor(() => expect(findState(key)).toEqual({ status: 'none' }))
      expect(open).not.toHaveBeenCalled()
      expect(findState(key)).toEqual({ status: 'none' })
    } finally { off() }
  })

  it('aborts its probe when the step goes away, restoring what it opened and dropping the verdict', async () => {
    const q = { label: 'Deep' }
    const key = 'probe-key'
    let opened!: () => void
    const wasOpened = new Promise<void>(r => { opened = r })
    const restored = vi.fn()
    let deep: HTMLButtonElement | null = null
    function Host() {
      useFindProbe({ runId: 'r', query: q, findKey: key, enabled: true })
      return <button data-testid="box">Box</button>
    }
    const { unmount } = render(<MemoryRouter><Host /></MemoryRouter>)
    const off = registerProbeTarget({
      kind: 'popup',
      trigger: () => document.querySelector('[data-testid="box"]'),
      isOpen: () => !!deep,
      open: () => {
        deep = document.createElement('button')
        deep.textContent = 'Deep'
        document.body.append(deep)
        opened()
        return () => { restored(); deep?.remove(); deep = null }
      },
    })
    try {
      await wasOpened
      unmount()
      await waitFor(() => expect(restored).toHaveBeenCalledTimes(1))
      await waitFor(async () => expect((await import('./findByName')).isProbing()).toBe(false))
      expect(restored).toHaveBeenCalledTimes(1)
      expect(findState(key)?.status).not.toBe('found')
    } finally { off() }
  })
})

describe('the container path, picks, and what the gateway hears', () => {
  it('points at the deepest registered container the page shows', () => {
    render(<Menu label="More"><DropdownMenuItem>Export</DropdownMenuItem></Menu>)
    const [entry] = probeTargets()
    const el = resolveContainerPath([{ id: entry.id, name: 'More', role: 'button', kind: 'popup' }, { id: -1, name: 'Advanced', role: 'button', kind: 'popup' }])
    expect(el?.textContent).toBe('More')
    // Remounted: re-found by name and role among the registered triggers.
    expect(resolveContainerPath([{ id: -2, name: 'More', role: 'button', kind: 'popup' }])?.textContent).toBe('More')
  })

  it('resolves the candidate the person picked, and keeps the candidates when they leave the screen', () => {
    const { unmount } = render(<><section aria-label="Daily"><button>Run</button></section><section aria-label="Weekly"><button data-testid="w">Run</button></section></>)
    expect(resolveFind({ label: 'Run' }, 'k')).toBeNull()
    expect(findCandidates({ label: 'Run' }, 'k')).toEqual(['Daily', 'Weekly'])
    setFindPick('k', 2)
    expect(resolveFind({ label: 'Run' }, 'k')?.dataset.testid).toBe('w')
    expect(findReport({ label: 'Run' }, 'k')).toMatchObject({ result: 'found', count: 1 })
    unmount()
    expect(findCandidates({ label: 'Run' }, 'k')).toEqual(['Daily', 'Weekly'])
  })

  it('keeps a pick on the control the person chose, and asks again once the list changes', () => {
    const Rows = ({ order }: { order: string[] }) => <>{order.map(n => <section key={n} aria-label={n}><button data-testid={n}>Run</button></section>)}</>
    const { rerender } = render(<Rows order={['Daily', 'Weekly', 'Monthly']} />)
    const q = { label: 'Run' }
    findCandidates(q, 'k')
    setFindPick('k', 2)
    expect(resolveFind(q, 'k')?.dataset.testid).toBe('Weekly')
    expect(findNeedsPick(q, 'k')).toBe(false)
    // Reordered: the second row is now another job. Never re-bound by number.
    rerender(<Rows order={['Weekly', 'Daily', 'Monthly']} />)
    expect(resolveFind(q, 'k')).toBeNull()
    expect(findNeedsPick(q, 'k')).toBe(true)
    expect(findReport(q, 'k')).toEqual({ result: 'ambiguous', count: 3 })
    // Only one left, and it is not the picked one: asked again, not taken.
    rerender(<Rows order={['Daily']} />)
    expect(resolveFind(q, 'k')).toBeNull()
    expect(findNeedsPick(q, 'k')).toBe(true)
    expect(findCandidates(q, 'k')).toEqual(['Daily'])
    setFindPick('k', 1)
    expect(resolveFind(q, 'k')?.dataset.testid).toBe('Daily')
  })

  it('reports ids and counts, never the names on the page', () => {
    render(
      <>
        <button {...marks({ location: "chat.older-sessions" })}>Older Sessions</button>
        <section aria-label="Secret project"><button>Copy</button><button>Copy</button></section>
      </>,
    )
    const found = findReport({ label: 'Older Sessions' }, 'k1')
    expect(found).toEqual({ result: 'found', count: 1, role: 'button', location_id: 'chat.older-sessions', label_key: expect.any(String) })
    const amb = findReport({ label: 'Copy' }, 'k2')
    expect(amb).toEqual({ result: 'ambiguous', count: 2 })
    expect(JSON.stringify([found, amb])).not.toMatch(/Older Sessions|Secret|Copy/)
    setFindState('k3', { status: 'none' })
    expect(findReport({ label: 'Nowhere' }, 'k3')).toEqual({ result: 'none', count: 0 })
    setFindState('k4', { status: 'found', path: [], count: 3 })
    expect(findReport({ label: 'Nowhere' }, 'k4')).toEqual({ result: 'ambiguous', count: 3 })
  })
})

describe('the ui.find action', () => {
  const action = (params: Record<string, unknown>) => resolveGuideAction({ id: 'ui.find', params }, 'g1', 0)

  it('resolves to an open step and a show step on the named page', () => {
    const r = action({ route: '/schedule', label: 'Run Now', role: 'button' })
    expect(r.ok).toBe(true)
    if (!r.ok) return
    expect(r.action.steps.map(s => s.target.kind)).toEqual(['find-container', 'find'])
    expect(r.action.steps[0].complete).toMatchObject({ kind: 'reach' })
    expect(r.action.steps[1].complete).toEqual({ kind: 'ack' })
    expect(r.action.enter.to({ pathname: '/chat', search: '' })).toBe('/schedule')
    expect(r.action.enter.to({ pathname: '/schedule', search: '?x=1' })).toBe('/schedule?x=1')
  })

  it('carries caution to the show step', () => {
    const r = action({ route: '/schedule', label: 'Delete', caution: true })
    expect(r.ok && r.action.steps[1].caution).toBe(true)
  })

  it('refuses a trust-root page and malformed params', () => {
    expect(action({ route: '/settings/security', label: 'Profiles' })).toEqual({ ok: false, reason: 'sensitive_page' })
    expect(action({ label: 'X', role: 'slider' })).toEqual({ ok: false, reason: 'invalid_params' })
    expect(action({ label: '' })).toEqual({ ok: false, reason: 'invalid_params' })
    expect(action({ label: 'X', route: '//evil' })).toEqual({ ok: false, reason: 'invalid_params' })
    expect(action({ label: 'X', selector: '#x' })).toEqual({ ok: false, reason: 'invalid_params' })
    expect(action({ label: 'X', location_id: 'Not An Id' })).toEqual({ ok: false, reason: 'invalid_params' })
    expect(action({ label: 'X', location_id: 7 })).toEqual({ ok: false, reason: 'invalid_params' })
  })

  it('carries a find_ref location id into the search', () => {
    const r = action({ label: 'Slack', role: 'tab', location_id: 'settings.sub.channels.slack' })
    expect(r.ok).toBe(true)
    if (!r.ok) return
    const t = r.action.steps[1].target
    expect(t.kind === 'find' && t.query).toEqual({ label: 'Slack', role: 'tab', location: 'settings.sub.channels.slack' })
  })

  it('refuses a destructive or trust-root control as an opener, and an opener without its control', () => {
    // The exact repro: Clear all passed off as what opens Mark all as read's menu.
    expect(action({ label: 'Mark all as read', location_id: 'notifications.mark-all-read', opener: 'notifications.clear-all' })).toEqual({ ok: false, reason: 'unknown_location' })
    expect(action({ label: 'X', location_id: 'notifications.mark-all-read', opener: 'settings.tab.security' })).toEqual({ ok: false, reason: 'unknown_location' })
    expect(action({ label: 'Mark all as read', opener: 'shell.notifications' })).toEqual({ ok: false, reason: 'invalid_params' })
  })

  it('carries a find_ref opener to the open step only, and refuses a malformed one', () => {
    const r = action({ label: 'Mark all as read', location_id: 'notifications.mark-all-read', opener: 'shell.notifications' })
    expect(r.ok && r.action.steps[0].target).toMatchObject({ kind: 'find-container', opener: 'shell.notifications' })
    expect(r.ok && r.action.steps[1].target).not.toHaveProperty('opener')
    expect(action({ label: 'X', opener: 'Not An Id' })).toEqual({ ok: false, reason: 'invalid_params' })
    expect(action({ label: 'X', opener: 7 })).toEqual({ ok: false, reason: 'invalid_params' })
    expect(action({ label: 'X', location_id: 'shell.notifications', opener: 'shell.notifications' })).toEqual({ ok: false, reason: 'invalid_params' })
  })
})

describe('a registered control, by its location id', () => {
  it('is found whatever its label reads in the page\'s state', () => {
    // An app owns the quick-search slot: the top bar's Search is relabelled.
    render(<button aria-label="Open command bar" {...marks({ location: "shell.search" })}>⌕</button>)
    const q = { label: 'Search sessions, files, and commands', role: 'button' as const }
    expect(searchByName(q).result).toBe('none')
    const r = searchByName({ ...q, location: 'shell.search' })
    expect(r.result).toBe('found')
    expect(r.element?.getAttribute('aria-label')).toBe('Open command bar')
  })

  it('finds a Settings sub-page row whose name carries its status line', () => {
    render(
      <MemoryRouter initialEntries={['/settings/channels/slack']}>
        <SettingsSubNav
          items={[
            { key: 'slack', label: 'Slack', summary: <span>Not configured</span> },
            { key: 'teams', label: 'Microsoft Teams', summary: <span>Connected</span> },
          ]}
          listLabel="Chat channels"
          basePath="/settings"
          guideTargetPrefix="settings.sub.channels."
        >
          {() => <button>Open the Slack app directory</button>}
        </SettingsSubNav>
      </MemoryRouter>,
    )
    const q = { label: 'Slack', role: 'tab' as const }
    // By name: "Slack Not configured" is no exact match, and two names contain it.
    expect(searchByName(q).result).toBe('none')
    const r = searchByName({ ...q, location: 'settings.sub.channels.slack' })
    expect(r.result).toBe('found')
    expect(r.element?.getAttribute('role')).toBe('option')
  })

  it('is still refused when it sits in a trust-root region', () => {
    render(<div {...guideTrustRoot}><button {...marks({ location: "shell.search" })}>Search</button></div>)
    expect(searchByName({ label: 'Search', location: 'shell.search' }).result).toBe('sensitive')
  })

  it('falls back to the name when the page draws no control carrying the id', () => {
    render(<button>Run Now</button>)
    expect(searchByName({ label: 'Run Now', location: 'schedule.run-now' }).result).toBe('found')
  })
})

describe('tracking a ui.find step', () => {
  function Tracked({ params, onMissing, onObserved, index = 0 }: { params: Record<string, unknown>; onMissing: (d?: string) => void; onObserved: () => void; index?: number }) {
    const r = resolveGuideAction({ id: 'ui.find', params }, 'g1', 0)
    const step = r.ok ? r.action.steps[index] : null
    useGuideStepTracker({ stepId: 's', step, enabled: true, suppressMissing: false, reduceMotion: true, onObserved, onMissing })
    return null
  }

  it('passes the open step at once when the control is already visible', () => {
    vi.useFakeTimers()
    try {
      const onObserved = vi.fn(() => true)
      render(<><button>Run Now</button><Tracked params={{ label: 'Run Now' }} onObserved={onObserved} onMissing={() => {}} /></>)
      act(() => { vi.advanceTimersByTime(300) })
      expect(onObserved).toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })

  it('reports not_found soon after the search concluded nothing carries the name', () => {
    vi.useFakeTimers()
    try {
      const onMissing = vi.fn((_d?: string) => true)
      render(<Tracked params={{ label: 'Nowhere' }} onObserved={() => {}} onMissing={onMissing} />)
      const key = JSON.stringify(['g1', 0, 'Nowhere', null, null])
      setFindState(key, { status: 'none' })
      act(() => { vi.advanceTimersByTime(GUIDE_EARLIER_STEP_WAIT_MS + 500) })
      expect(onMissing).toHaveBeenCalledWith('not_found')
    } finally { vi.useRealTimers() }
  })

  it('reports a ceiling match as not_found and never points at it', () => {
    vi.useFakeTimers()
    try {
      const onMissing = vi.fn((_d?: string) => true)
      const onObserved = vi.fn(() => true)
      render(<><div {...guideTrustRoot}><button>Approve</button></div><Tracked params={{ label: 'Approve' }} onObserved={onObserved} onMissing={onMissing} /></>)
      act(() => { vi.advanceTimersByTime(GUIDE_EARLIER_STEP_WAIT_MS + 500) })
      expect(onObserved).not.toHaveBeenCalled()
      expect(onMissing).toHaveBeenCalledWith('not_found')
    } finally { vi.useRealTimers() }
  })

  it('reports ambiguous when several visible controls carry the name, and points at a picked one', () => {
    vi.useFakeTimers()
    try {
      const onMissing = vi.fn((_d?: string) => true)
      const onObserved = vi.fn(() => true)
      render(<><button>Copy</button><button>Copy</button><Tracked params={{ label: 'Copy' }} onObserved={onObserved} onMissing={onMissing} /></>)
      act(() => { vi.advanceTimersByTime(GUIDE_EARLIER_STEP_WAIT_MS + 500) })
      expect(onMissing).toHaveBeenCalledWith('ambiguous')
      const key = JSON.stringify(['g1', 0, 'Copy', null, null])
      findCandidates({ label: 'Copy' }, key)
      setFindPick(key, 2)
      // Offered on the reports' own retry cadence after the missing report.
      act(() => { vi.advanceTimersByTime(GUIDE_FOUND_RETRY_MS + 300) })
      expect(onObserved).toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })

  it('passes the open step once the person opens the container holding several matches', () => {
    vi.useFakeTimers()
    try {
      const onObserved = vi.fn(() => true)
      const key = JSON.stringify(['g1', 0, 'Copy', null, null])
      setFindState(key, { status: 'found', path: [{ id: -1, name: 'More', role: 'button', kind: 'popup' }], count: 2 })
      const { rerender } = render(<Tracked params={{ label: 'Copy' }} onObserved={onObserved} onMissing={() => {}} />)
      act(() => { vi.advanceTimersByTime(300) })
      expect(onObserved).not.toHaveBeenCalled()
      rerender(<><button>Copy</button><button>Copy</button><Tracked params={{ label: 'Copy' }} onObserved={onObserved} onMissing={() => {}} /></>)
      act(() => { vi.advanceTimersByTime(300) })
      expect(onObserved).toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })
})
