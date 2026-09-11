/**
 * Isolated capture entry for the appearance-pack avatar tier.
 *
 * WHY ISOLATED: the Library tab is three levels down in the live app (crew
 * editor → Customize avatar → tab) and its grid needs a seeded pack library the
 * dashboard only has once a pack has been imported. Mounted directly, the real
 * components render the real output with no gateway and no config; the harness
 * answers `/api/appearances*` at the network so the cards' thumbnails are served
 * exactly the way they are in the product.
 *
 * Scene comes from the query string: ?scene=library|faces|formats, ?theme=dark|light
 */
import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n/all'
import CrewAvatar, { type CrewAvatarOverride } from '../src/components/CrewAvatar'
import CrewAvatarBuilder from '../src/components/CrewAvatarBuilder'
import type { AvatarFaceState } from '../src/lib/crewAvatarState'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'library'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** A crew already wearing a pack, with a sound on two of its moments — the
 *  reaction layer a pack keeps. */
const AURORA_CREW: CrewAvatarOverride = {
  kind: 'pack',
  id: 'aurora',
  sounds: { done: 'chime', error: 'pop' },
}

/** One crew per pack FORMAT, each wearing art the matching player has to draw:
 *  an `<img>` for the svg, `LottieRenderer` for the lottie document, and
 *  `SpriteRenderer` stepping the row the sheet assigns to that slot. */
const FORMAT_CREWS = [
  { id: 'aurora', label: 'svg — an <img> on the slot route', seed: 'aurora-crew' },
  { id: 'nebula', label: 'lottie — LottieRenderer', seed: 'nebula-crew' },
  { id: 'pixels', label: 'sprite — SpriteRenderer, row per slot', seed: 'pixels-crew' },
] as const

function Formats() {
  const order: AvatarFaceState[] = ['idle', 'working']
  // No `min-h-screen`: this scene is photographed full-page, and stretching to the
  // viewport left most of the frame as empty background below the content.
  return (
    <div className="bg-bg p-10 pb-4 text-text">
      <h1 className="mb-1 text-[15px] font-semibold">Every format a crew can wear</h1>
      <p className="mb-8 text-[12px] text-muted">
        The pack is read once, and the player is chosen from the format of the slot the state
        resolves to. A sprite steps the row its sheet assigns to that slot, so idle and working are
        two different clips out of one sheet.
      </p>
      {FORMAT_CREWS.map(({ id, label, seed }) => (
        <div key={id} className="mb-9 border-t border-border pt-7 first:border-t-0 first:pt-0">
          <h2 className="mb-3 text-[12.5px] font-medium">{label}</h2>
          <div className="flex flex-wrap gap-8">
            {order.map(state => (
              <div key={state} className="flex w-[150px] flex-col items-center gap-3">
                <CrewAvatar seed={seed} avatar={{ kind: 'pack', id }} state={state} size={104} />
                <span className="text-[11.5px] text-muted">{state}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
      <hr className="mb-8 mt-2 border-border" />
      <h2 className="mb-3 text-[12.5px] font-medium">Roster size: the same three packs at 38px</h2>
      <p className="mb-5 text-[12px] text-muted">
        A dense list holds dozens of these. A lottie or sprite avatar plays only while its own box
        is in the viewport and its state is a reaction; off screen, or at idle, it holds frame 0.
        The svg tier is a plain image.
      </p>
      <div className="flex flex-wrap items-center gap-6">
        {FORMAT_CREWS.map(({ id, seed }) => (
          <div key={id} className="flex items-center gap-2">
            <CrewAvatar seed={seed} avatar={{ kind: 'pack', id }} state="working" size={38} />
            <span className="text-[12px]">{seed}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** Caption per state — plain English, not catalog copy: this page is evidence,
 *  not a shipped surface, and a translated label would photograph as whatever
 *  locale the harness happened to boot. */
const CAPTIONS: Record<AvatarFaceState, string> = {
  idle: 'idle — /slot/idle',
  working: 'working — /slot/working',
  done: 'done — /slot/done',
  error: 'error — /slot/error',
}

function Faces() {
  const order: AvatarFaceState[] = ['idle', 'working', 'done', 'error']
  return (
    <div className="bg-bg p-10 pb-6 text-text">
      <h1 className="mb-1 text-[15px] font-semibold">A crew wearing a pack</h1>
      <p className="mb-6 text-[12px] text-muted">
        Each moment asks the gateway for its own slot. The pack draws only some of them; the server
        resolves the rest back to idle.
      </p>
      <div className="flex flex-wrap gap-8">
        {order.map(state => (
          <div key={state} className="flex w-[170px] flex-col items-center gap-3">
            <CrewAvatar seed="aurora-crew" avatar={AURORA_CREW} state={state} size={120} />
            <span className="text-center text-[11.5px] text-muted">{CAPTIONS[state]}</span>
          </div>
        ))}
      </div>
      <hr className="mb-10 mt-14 border-border" />
      <h2 className="mb-3 text-[13px] font-semibold">Drawn locally: the built-in pack, and a pack that is gone</h2>
      <p className="mb-6 text-[12px] text-muted">
        Neither of these fetches anything. The built-in kiro-ghost IS the name-derived ghost, and a
        pack whose art fails to load falls back to that same drawing. It is seeded by the CREW's
        name, so two crews get two different faces — and neither is a broken-image glyph.
      </p>
      <div className="flex flex-wrap gap-12">
        {[
          { seed: 'aurora-crew', id: 'kiro-ghost', caption: 'aurora-crew wearing the built-in pack' },
          { seed: 'sage', id: 'deleted-pack', caption: 'sage wearing a pack that is gone' },
        ].map(({ seed, id, caption }) => (
          <div key={id} className="flex w-[230px] flex-col items-center gap-3">
            <CrewAvatar seed={seed} avatar={{ kind: 'pack', id }} size={92} />
            <span className="text-center text-[11.5px] text-muted">{caption}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function Builder() {
  const [value, setValue] = useState<CrewAvatarOverride | null>(AURORA_CREW)
  return (
    <div className="min-h-screen bg-bg text-text">
      <CrewAvatarBuilder
        open
        name="aurora-crew"
        value={value}
        onCancel={() => {}}
        onSave={next => setValue(next)}
      />
    </div>
  )
}

initI18n('en')
const SCENES: Record<string, () => React.JSX.Element> = {
  faces: Faces,
  formats: Formats,
  library: Builder,
}
const Scene = SCENES[scene] ?? Builder
// The pack tier reads through React Query, like the live app.
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <Scene />
  </QueryClientProvider>,
)
