/**
 * When the picker offers `Auto (Jev)`, and when it reads as selected.
 *
 * Both gates are asserted as FAIL-CLOSED, which is the whole point of the module:
 * the fleet's `decisions_enabled` and the owner's `permits` are different answers,
 * and offering the row on either alone puts a paid third-party call one click away
 * from someone who did not agree to one. Neither read is an enforcement point — the
 * gate re-checks both — so these tests pin presentation, not security.
 *
 * The owner's side is `permits`, never the keystone's bare `enabled`: `permits` is
 * the answer the GATE will give, because it also holds the consented endpoint
 * against the one config names now and folds in the governance denial. A surface
 * reading `enabled` offers a control that routes nothing whenever those differ.
 *
 * The selected-state half matters just as much: a routed slot stores `model: 'auto'`,
 * so a picker highlighting on the model alone would tick the Auto row and name a
 * behaviour the session does not have.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { render, screen, cleanup } from '@testing-library/react'
import { describe, it, expect, afterEach } from 'vitest'

import ModelDropdownList from '../components/ModelDropdownList'

import {
  isUnpinnedModel,
  JEV_ROUTE_MODEL,
  jevRouteOffered,
  jevRouteShownModel,
  withJevRoute,
} from '../lib/jevRoute'

const LIST = [{ name: 'auto', description: '' }, { name: 'claude-opus-5', description: 'x' }]
const LABEL = 'let Jev judge each turn'

describe('jevRouteOffered', () => {
  it('needs the fleet answer AND the owner consent', () => {
    expect(jevRouteOffered({ decisions_enabled: true }, { permits: true }, true)).toBe(true)
  })

  it('is refused when either one is missing, false, or not yet read', () => {
    // An absent field is an older gateway or a read that has not landed; neither
    // is permission.
    for (const cfg of [undefined, {}, { decisions_enabled: false }]) {
      expect(jevRouteOffered(cfg, { permits: true }, true)).toBe(false)
    }
    for (const consent of [undefined, {}, { permits: false }]) {
      expect(jevRouteOffered({ decisions_enabled: true }, consent, true)).toBe(false)
    }
  })

  it('is refused for a keystone consented to a DIFFERENT provider endpoint', () => {
    // The redirected-config state: the owner did consent, so `enabled` is true, but
    // the address recorded is not the one config names now and the gate refuses. A
    // surface reading `enabled` would offer the row and let the chip claim a routed
    // turn the backend declined -- which is exactly what `permits` folds in.
    expect(
      jevRouteOffered(
        { decisions_enabled: true },
        { enabled: true, permits: false } as { permits?: boolean },
        true,
      ),
    ).toBe(false)
  })

  it('reads only an exact `true` on either side', () => {
    // The gateway emits a real boolean and the gate accepts nothing else, so a
    // hand-edited "true" must not read as consent here either.
    expect(jevRouteOffered({ decisions_enabled: 1 as never }, { permits: true }, true)).toBe(
      false,
    )
    expect(
      jevRouteOffered({ decisions_enabled: true }, { permits: 'true' as never }, true),
    ).toBe(false)
  })
})

describe('isUnpinnedModel', () => {
  it('reads both spellings of "no model", whatever their case or padding', () => {
    // `''` is a freshly dispatched slot, `auto` is the picker's inherit row, and the
    // value arrives from a client -- so a near-miss that read as a pin would turn
    // the feature off for that session with nothing said.
    for (const model of ['', 'auto', 'AUTO', '  auto  ', undefined]) {
      expect(isUnpinnedModel(model)).toBe(true)
    }
  })

  it('reads any named model as pinned, including the sentinel', () => {
    // The sentinel is resolved to `auto` at the top of the model handler, so a slot
    // still holding it is a bug; reading it as unpinned would hide that bug.
    for (const model of ['claude-opus-5', 'auto:jev', 'autopilot', 'auto-1']) {
      expect(isUnpinnedModel(model)).toBe(false)
    }
  })

  it('agrees with the gate that actually routes', () => {
    // The Python half is `_JEV_ROUTE_AUTO_MODELS` in `dashboard/chat_runner.py`. A
    // chip keyed off a different set would say `Auto` for a turn that routed, which
    // is the one thing this pair exists to prevent.
    const runner = readFileSync(
      join(__dirname, '../../../src/kiro_crew/dashboard/chat_runner.py'),
      'utf8',
    )
    expect(runner).toContain('_JEV_ROUTE_AUTO_MODELS = ("", "auto")')
  })
})

describe('a picker with no live slot', () => {
  it('does not offer the row, and cannot hold the sentinel as a pending model', () => {
    // A pick made with no slot is HELD as the pending model and forwarded verbatim
    // into slot creation, which stores the id as given. So the sentinel would land in
    // the new slot's `model` — an id no provider serves — with routing off, which is
    // the one place `slot.model` must never reach. Both halves are pinned: the offer
    // needs a slot, and the holding branch resolves the sentinel even so.
    const cfg = { decisions_enabled: true }
    const consent = { permits: true }
    expect(jevRouteOffered(cfg, consent, false)).toBe(false)
    expect(jevRouteOffered(cfg, consent, true)).toBe(true)
    for (const rel of ['../pages/ChatPage.tsx', '../components/ChatPane.tsx']) {
      const source = readFileSync(join(__dirname, rel), 'utf-8')
      const line = source.split('\n').find(l => l.includes('jevRouteOffered('))
      expect(line, `${rel} offers Auto (Jev) without asking for a slot`).toMatch(/Slot\)/)
    }
    // Held as nothing rather than as a resolved id: the create-value guard in
    // ChatPage.newSessionModel.test.ts admits only an explicit pick or an empty
    // string, and an omitted model is what lets the backend resolve the chain.
    const page = readFileSync(join(__dirname, '../pages/ChatPage.tsx'), 'utf-8')
    expect(page, 'the no-slot branch holds the sentinel').toContain(
      "if (modelName === JEV_ROUTE_MODEL) { setPendingModel(''); return }",
    )
  })
})

describe('a remote-bound session', () => {
  it('is withheld at the call site, not inside jevRouteOffered', () => {
    // The two pages AND the condition: a peer-bound session's turns run on the
    // other machine through `relay_remote_turn` and never reach the routing hook,
    // so the entry would be a control that silently does nothing. Asserted on the
    // source because the condition is a conjunct at the call site — `jevRouteOffered`
    // answers only the consent question and knows nothing about remoteness.
    for (const rel of ['../pages/ChatPage.tsx', '../components/ChatPane.tsx']) {
      const source = readFileSync(join(__dirname, rel), 'utf-8')
      const line = source.split('\n').find(l => l.includes('jevRouteOffered('))
      expect(line, `${rel} does not call jevRouteOffered`).toBeTruthy()
      expect(line, `${rel} offers Auto (Jev) on a remote slot`).toMatch(/isRemote/)
    }
  })
})

describe('withJevRoute', () => {
  it('prepends the row beside Auto, which is its sibling', () => {
    const list = withJevRoute(LIST, true, LABEL)
    expect(list.map(m => m.name)).toEqual([JEV_ROUTE_MODEL, 'auto', 'claude-opus-5'])
    expect(list[0].description).toBe(LABEL)
  })

  it('returns the SAME array when the row is not offered', () => {
    // Identity, not just equality: the caller passes the list through
    // unconditionally, and a fresh array every render would defeat the memo on
    // React Query's cached value.
    expect(withJevRoute(LIST, false, LABEL)).toBe(LIST)
  })

  it('does not add the row twice', () => {
    const once = withJevRoute(LIST, true, LABEL)
    expect(withJevRoute(once, true, LABEL)).toBe(once)
  })

  it('is not a provider model id', () => {
    // No advertised id carries a colon, which is what keeps the sentinel outside
    // the model namespace; the prefix is `auto` so a client too old to know the
    // row reads it as the Auto it behaves like.
    expect(JEV_ROUTE_MODEL).toBe('auto:jev')
    expect(LIST.some(m => m.name === JEV_ROUTE_MODEL)).toBe(false)
  })
})

describe('jevRouteShownModel', () => {
  it('names the routed row for a routed slot, whatever the model says', () => {
    // A routed slot stores `auto`, so the model alone would tick the Auto row.
    expect(jevRouteShownModel('auto', { jev_route: true })).toBe(JEV_ROUTE_MODEL)
    expect(jevRouteShownModel('claude-opus-5', { jev_route: true })).toBe(JEV_ROUTE_MODEL)
  })

  it('leaves every other slot exactly as the display answer had it', () => {
    for (const slot of [undefined, {}, { jev_route: false }]) {
      expect(jevRouteShownModel('claude-opus-5', slot)).toBe('claude-opus-5')
      expect(jevRouteShownModel('auto', slot)).toBe('auto')
    }
  })

  it('reads only an exact `true`', () => {
    expect(jevRouteShownModel('auto', { jev_route: 1 as never })).toBe('auto')
  })
})

describe('the picker row', () => {
  afterEach(cleanup)

  it('shows a label, never the wire id', () => {
    // The id exists so the gateway can recognise the choice; showing it would put
    // an internal spelling where a user expects a name.
    render(
      <ModelDropdownList
        models={withJevRoute(LIST, true, LABEL)}
        activeModel={JEV_ROUTE_MODEL}
        onSelect={() => {}}
      />,
    )
    expect(screen.getByText('Auto (Jev)')).toBeInTheDocument()
    expect(screen.queryByText(JEV_ROUTE_MODEL)).toBeNull()
  })

  it('keeps every real model id verbatim, because that is what users match on', () => {
    render(<ModelDropdownList models={LIST} activeModel="auto" onSelect={() => {}} />)
    expect(screen.getByText('claude-opus-5')).toBeInTheDocument()
    expect(screen.getByText('auto')).toBeInTheDocument()
  })

  it('carries the id as an attribute, so a selector is not locale-dependent', () => {
    const { container } = render(
      <ModelDropdownList
        models={withJevRoute(LIST, true, LABEL)}
        activeModel={JEV_ROUTE_MODEL}
        onSelect={() => {}}
      />,
    )
    const ids = [...container.querySelectorAll('[data-model-id]')].map(el => el.getAttribute('data-model-id'))
    expect(ids).toEqual([JEV_ROUTE_MODEL, 'auto', 'claude-opus-5'])
  })
})
