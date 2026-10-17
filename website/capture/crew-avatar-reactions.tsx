/**
 * Isolated capture entry for the crew avatar's reaction layer.
 *
 * WHY ISOLATED: both surfaces need state the SPA only reaches live. The builder's
 * Reactions tab is three levels down (crew editor → Customize avatar → tab), and
 * the reacting faces need a member slot that is actually running and then actually
 * stops. Mounted directly, the same two components render the same output with no
 * gateway, no websocket, and no seeded config — every face here is composed by the
 * real `compose()` through the real style module, so the screenshot shows what the
 * roster shows.
 *
 * A reaction is an ANIMATION, so a still frame is weak evidence of it by nature:
 * the states scene therefore also prints each face's chosen motion, and the
 * capture script asserts the animation is present in the composed markup. The
 * moving proof is the recorded flow in the PR.
 *
 * Scene comes from the query string: ?scene=builder|states, ?theme=dark|light,
 * ?retiredCue=image|pack (the builder as opened on a crew whose SAVED record, on
 * that tier, still names a preset sound the tier no longer plays — the one
 * state that shows that tier's line, and only that tier's).
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n/all'
import CrewAvatar, { type CrewAvatarOverride } from '../src/components/CrewAvatar'
import CrewAvatarBuilder from '../src/components/CrewAvatarBuilder'
import type { AvatarFaceState, RetiredCue } from '../src/lib/crewAvatarState'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'builder'
const theme = params.get('theme') || 'dark'
/* A realistic legacy record: the two states a crew most often had a chime
   on, so the notice photographs with real state names in it. The tier comes
   from the query so each served-tier frame is of a crew saved ON that tier. */
const retiredCueKind = params.get('retiredCue')
const retiredCue: RetiredCue | null =
  retiredCueKind === 'image' || retiredCueKind === 'pack'
    ? { kind: retiredCueKind, states: ['done', 'error'] }
    : null

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** A crew that pinned a face AND chose a reaction for both moments. */
const RADAR: CrewAvatarOverride = {
  kind: 'ghost',
  traits: {
    eyes: 'canon',
    brows: 'flat',
    mouth: 'smile',
    accessory: 'phones',
    prop: 'mug',
    blush: true,
    flip: false,
    tile: '#259d85',
  },
  motions: { done: 'sparkle', error: 'droop' },
  sounds: { working: 'blip', done: 'chime', error: 'pop' },
}

/** Caption per state — plain English, not catalog copy: this page is evidence,
 *  not a shipped surface, and a translated label would photograph as whatever
 *  locale the harness happened to boot. */
const CAPTIONS: Record<AvatarFaceState, string> = {
  idle: 'idle',
  working: 'working',
  done: 'done · sparkle',
  error: 'error · droop',
}

/** What the SAME four states are for a crew that configured nothing. Kept to
 *  one short line each: a caption that wraps while its neighbours do not makes
 *  the row read as misaligned in the screenshot. */
const DEFAULT_CAPTIONS: Record<AvatarFaceState, string> = {
  idle: 'idle',
  working: 'working',
  done: 'done · bounce',
  error: 'error · shake',
}

function States() {
  const order: AvatarFaceState[] = ['idle', 'working', 'done', 'error']
  return (
    <div className="min-h-screen bg-bg p-10 text-text">
      <h1 className="mb-1 text-[15px] font-semibold">One crew, four moments</h1>
      <p className="mb-6 max-w-[720px] text-[12px] text-muted">
        Identity never moves: same headphones, same mug, same tile. A reaction moves the ghost and
        may change its eyes — nothing else. The animated and rising frames are drawn slightly
        smaller and lower on purpose: that is the headroom a tall accessory needs to stay on the
        tile while the ghost bobs or hops.
      </p>
      <div className="flex flex-wrap gap-10">
        {order.map(state => (
          <div key={state} className="flex w-[150px] flex-col items-center gap-3">
            <CrewAvatar seed="radar" avatar={RADAR} state={state} working="full" size={132} />
            <span className="text-center text-[11.5px] text-muted">{CAPTIONS[state]}</span>
          </div>
        ))}
      </div>
      <h2 className="mb-3 mt-14 text-[13px] font-semibold">A crew that chose nothing</h2>
      <p className="mb-4 text-[12px] text-muted">
        The layer is on by default: with no override at all the name-derived face still bounces when
        a turn finishes and shakes when one fails. The working and reacting frames animate.
      </p>
      <div className="flex flex-wrap gap-10">
        {order.map(state => (
          <div key={state} className="flex w-[110px] flex-col items-center gap-2">
            <CrewAvatar seed="sage" state={state} working="full" size={88} />
            <span className="text-[11px] text-muted">{DEFAULT_CAPTIONS[state]}</span>
          </div>
        ))}
      </div>
      <h2 className="mb-3 mt-14 text-[13px] font-semibold">Every built-in reaction</h2>
      <p className="mb-4 text-[12px] text-muted">
        Done: still, bounce, nod, sparkle. Error: still, shake, cross-eyes, droop.
      </p>
      {(
        [
          ['done', ['none', 'bounce', 'nod', 'sparkle']],
          ['error', ['none', 'shake', 'cross-eyes', 'droop']],
        ] as const
      ).map(([state, names]) => (
        <div key={state} className="mb-5 flex flex-wrap gap-5">
          {names.map(name => (
            <div key={name} className="flex w-[104px] flex-col items-center gap-2">
              <CrewAvatar
                seed="radar"
                avatar={{ ...RADAR, motions: { [state]: name } } as CrewAvatarOverride}
                state={state}
                size={88}
              />
              <span className="text-[11px] text-muted">{`${state} · ${name}`}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

function Builder() {
  const [value, setValue] = useState<CrewAvatarOverride | null>(RADAR)
  return (
    <div className="min-h-screen bg-bg text-text">
      <CrewAvatarBuilder
        open
        name="radar"
        value={value}
        retiredCue={retiredCue}
        onCancel={() => {}}
        onSave={next => setValue(next)}
      />
    </div>
  )
}

initI18n('en')
/** The Library tab reads the pack list through React Query, so the harness has to
 *  provide the client the app shell normally does — without one the tab throws on
 *  mount and the pane photographs as an empty box. No retry: the capture script
 *  answers the route itself, and a retry ladder would only delay the frame. */
const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queries}>
    {scene === 'states' ? <States /> : <Builder />}
  </QueryClientProvider>,
)
