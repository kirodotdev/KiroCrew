/**
 * Isolated capture entry for the per-state crew avatar work.
 *
 * WHY ISOLATED: both surfaces need state the SPA only reaches live. The builder's
 * Expressions tab is three levels down (crew editor → Customize avatar → tab), and
 * the reacting faces need a member slot that is actually running and then actually
 * stops. Mounted directly, the same two components render the same output with no
 * gateway, no websocket, and no seeded config — every face here is composed by the
 * real `compose()` through the real style module, so the screenshot shows what the
 * roster shows.
 *
 * Scene comes from the query string: ?scene=builder|states, ?theme=dark|light
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import CrewAvatar, { type CrewAvatarOverride } from '../src/components/CrewAvatar'
import CrewAvatarBuilder from '../src/components/CrewAvatarBuilder'
import type { AvatarFaceState } from '../src/lib/crewAvatarState'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'builder'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** A crew that pinned a face AND gave all three moments their own reaction. */
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
  expressions: {
    working: { eyes: 'squint', mouth: 'smirk' },
    done: { eyes: 'sparkle', mouth: 'grin' },
    error: { eyes: 'cross', mouth: 'wobble' },
  },
  sounds: { working: 'blip', done: 'chime', error: 'pop' },
}

/** Caption per state — plain English, not catalog copy: this page is evidence,
 *  not a shipped surface, and a translated label would photograph as whatever
 *  locale the harness happened to boot. */
const CAPTIONS: Record<AvatarFaceState, string> = {
  idle: 'idle — the resting face',
  working: 'working — animated, its own eyes and mouth',
  done: 'done — flashes for four seconds',
  error: 'error — the last turn ended without a reply',
}

function States() {
  const order: AvatarFaceState[] = ['idle', 'working', 'done', 'error']
  return (
    <div className="min-h-screen bg-bg p-10 text-text">
      <h1 className="mb-1 text-[15px] font-semibold">One crew, four moments</h1>
      <p className="mb-6 text-[12px] text-muted">
        Identity never moves: same headphones, same mug, same tile. Only the eyes and mouth react.
      </p>
      <div className="flex flex-wrap gap-8">
        {order.map(state => (
          <div key={state} className="flex w-[190px] flex-col items-center gap-3">
            <CrewAvatar seed="radar" avatar={RADAR} state={state} working="full" size={132} />
            <span className="text-center text-[11.5px] text-muted">{CAPTIONS[state]}</span>
          </div>
        ))}
      </div>
      <h2 className="mb-3 mt-10 text-[13px] font-semibold">A crew that pinned no face</h2>
      <p className="mb-4 text-[12px] text-muted">
        Reactions alone are a valid override: the name-derived face keeps its identity and still
        reacts.
      </p>
      <div className="flex gap-6">
        {order.map(state => (
          <CrewAvatar
            key={state}
            seed="sage"
            avatar={{ kind: 'ghost', expressions: { working: { eyes: 'wide' }, done: { mouth: 'grin' }, error: { eyes: 'swirl' } } }}
            state={state}
            working="full"
            size={72}
          />
        ))}
      </div>
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
        onCancel={() => {}}
        onSave={next => setValue(next)}
      />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(scene === 'states' ? <States /> : <Builder />)
