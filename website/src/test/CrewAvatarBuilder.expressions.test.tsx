/**
 * The Expressions tier of the avatar builder.
 *
 * `CrewAvatarBuilder.test.tsx` covers the two identity tiers; this file covers
 * the reaction layer alone — the three state rows, the shape Apply hands the
 * editor, and the one rule the tier switch has to keep: expressions authored on
 * the ghost tier must survive a trip through the Picture tab, because silently
 * discarding them is indistinguishable from the builder forgetting them.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import CrewAvatarBuilder from '../components/CrewAvatarBuilder'
import type { CrewAvatarOverride } from '../components/CrewAvatar'
import { AVATAR_STATES } from '../lib/crewAvatarState'
import { playPreset } from '../hooks/useNotificationSound'

vi.mock('../hooks/useNotificationSound', async importOriginal => {
  const actual = await importOriginal<typeof import('../hooks/useNotificationSound')>()
  return {
    ...actual,
    playPreset: vi.fn(),
    loadSoundSettings: vi.fn(() => ({ enabled: true, volume: 0.4, perCategory: {} })),
  }
})

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition',
    'variants', 'whileHover', 'whileTap', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const cache = new Map<string, unknown>()
  return {
    motion: new Proxy({}, {
      get: (_t, tag: string) => {
        if (!cache.has(tag)) cache.set(tag, make(tag))
        return cache.get(tag)
      },
    }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

type Ghost = Extract<CrewAvatarOverride, { kind: 'ghost' }>
type Picture = Extract<CrewAvatarOverride, { kind: 'image' }>

function mount(value: CrewAvatarOverride | null = null) {
  const onSave = vi.fn()
  const onCancel = vi.fn()
  const utils = render(
    <CrewAvatarBuilder open name="radar" value={value} onCancel={onCancel} onSave={onSave} />,
  )
  return { ...utils, onSave, onCancel }
}

const openExpressions = () => fireEvent.click(screen.getByRole('button', { name: 'Expressions' }))
const openPicture = () => fireEvent.click(screen.getByRole('button', { name: 'Picture' }))
const openFace = () => fireEvent.click(screen.getByRole('button', { name: 'Ghost face' }))
const apply = () => fireEvent.click(screen.getByTestId('avatar-builder-save'))
const lastSaved = (onSave: ReturnType<typeof vi.fn>) =>
  onSave.mock.calls.at(-1)?.[0] as CrewAvatarOverride | null

/** Pick one state's eyes or mouth from its option row. */
const pick = (state: string, axis: 'eyes' | 'mouth', option: string) =>
  fireEvent.click(screen.getByTestId(`avatar-expr-opt-${state}-${axis}-${option}`))

/** Choose a sound from one state's themed dropdown. */
async function chooseSound(state: string, label: string) {
  const row = screen.getByTestId(`avatar-state-sound-${state}`)
  fireEvent.click(row.querySelector('[role="combobox"]') as HTMLElement)
  fireEvent.click(await screen.findByRole('option', { name: label }))
}

describe('CrewAvatarBuilder — Expressions tier', () => {
  it('renders one row per state, each with its own preview and pickers', () => {
    mount()
    openExpressions()
    expect(screen.getByTestId('avatar-expressions-pane')).toBeInTheDocument()
    for (const state of AVATAR_STATES) {
      expect(screen.getByTestId(`avatar-state-row-${state}`)).toBeInTheDocument()
      expect(screen.getByTestId(`avatar-state-preview-${state}`)).toBeInTheDocument()
      expect(screen.getByTestId(`avatar-expr-${state}-eyes`)).toBeInTheDocument()
      expect(screen.getByTestId(`avatar-expr-${state}-mouth`)).toBeInTheDocument()
      expect(screen.getByTestId(`avatar-state-sound-${state}`)).toBeInTheDocument()
    }
  })

  it('offers "same as usual" first on every axis, selected until something is picked', () => {
    mount()
    openExpressions()
    const idleOption = screen.getByTestId('avatar-expr-opt-working-eyes-idle')
    expect(idleOption).toHaveAttribute('aria-selected', 'true')
    const row = screen.getByTestId('avatar-expr-working-eyes')
    expect(row.firstElementChild).toBe(idleOption)
  })

  it('emits only the axes that were picked, under only the states that were touched', () => {
    const { onSave } = mount()
    openExpressions()
    pick('working', 'eyes', 'squint')
    pick('error', 'mouth', 'wobble')
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.kind).toBe('ghost')
    expect(saved.expressions).toEqual({ working: { eyes: 'squint' }, error: { mouth: 'wobble' } })
  })

  it('reactions alone are a complete override — no traits, so the seeded face stays', () => {
    // `{kind:'ghost'}` with no traits means "the name-derived face, plus these
    // reactions"; writing traits here would silently pin the drawn face.
    const { onSave } = mount()
    openExpressions()
    pick('done', 'mouth', 'grin')
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.traits).toBeUndefined()
    expect(saved.expressions).toEqual({ done: { mouth: 'grin' } })
  })

  it('re-picking "same as usual" on the last axis drops the state entirely', () => {
    const { onSave } = mount()
    openExpressions()
    pick('working', 'eyes', 'squint')
    pick('working', 'eyes', 'idle')
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })

  it('keeps the other axis when one is cleared', () => {
    const { onSave } = mount()
    openExpressions()
    pick('working', 'eyes', 'squint')
    pick('working', 'mouth', 'oh')
    pick('working', 'eyes', 'idle')
    apply()
    expect((lastSaved(onSave) as Ghost).expressions).toEqual({ working: { mouth: 'oh' } })
  })

  it('carries a chosen sound through Apply', async () => {
    const { onSave } = mount()
    openExpressions()
    await chooseSound('done', 'Chime')
    apply()
    expect((lastSaved(onSave) as Ghost).sounds).toEqual({ done: 'chime' })
  })

  it('the silent row clears a state back to no sound', async () => {
    const { onSave } = mount({ kind: 'ghost', sounds: { done: 'chime' } })
    openExpressions()
    await chooseSound('done', 'No sound')
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })

  it('previews a chosen sound through the shared synth, and cannot preview none', async () => {
    mount()
    openExpressions()
    const button = screen.getByTestId('avatar-state-sound-preview-working')
    expect(button).toBeDisabled()
    await chooseSound('working', 'Blip')
    fireEvent.click(screen.getByTestId('avatar-state-sound-preview-working'))
    expect(vi.mocked(playPreset)).toHaveBeenCalledWith('blip', 0.4)
  })

  it('the per-state reset clears that state and leaves the others alone', async () => {
    const { onSave } = mount()
    openExpressions()
    pick('working', 'eyes', 'squint')
    pick('done', 'mouth', 'grin')
    await chooseSound('working', 'Blip')
    fireEvent.click(screen.getByTestId('avatar-state-reset-working'))
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.expressions).toEqual({ done: { mouth: 'grin' } })
    expect(saved.sounds).toBeUndefined()
  })

  it('pre-fills from the stored record', () => {
    mount({ kind: 'ghost', expressions: { working: { eyes: 'wide' } }, sounds: { error: 'pulse' } })
    openExpressions()
    expect(screen.getByTestId('avatar-expr-opt-working-eyes-wide')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('avatar-state-sound-error')).toHaveTextContent('Pulse')
  })

  it('shows only the sounds for a picture, and says why', () => {
    mount({ kind: 'image', v: 2 })
    openExpressions()
    expect(screen.getByTestId('avatar-expressions-picture-note')).toBeInTheDocument()
    for (const state of AVATAR_STATES) {
      expect(screen.getByTestId(`avatar-state-sound-${state}`)).toBeInTheDocument()
      expect(screen.queryByTestId(`avatar-expr-${state}-eyes`)).toBeNull()
      expect(screen.queryByTestId(`avatar-state-preview-${state}`)).toBeNull()
    }
  })

  it('a picture keeps its sounds and stays a picture', async () => {
    const { onSave } = mount({ kind: 'image', v: 2 })
    openExpressions()
    await chooseSound('done', 'Ding')
    apply()
    const saved = lastSaved(onSave) as Picture
    expect(saved.kind).toBe('image')
    expect(saved.sounds).toEqual({ done: 'ding' })
  })

  it('clearing the last reaction on a saved picture actually clears it', () => {
    // No new picture is picked, so Apply hands back the STORED value -- which
    // still carries the saved reactions. They must not survive the clear.
    const { onSave } = mount({ kind: 'image', v: 2, sounds: { done: 'chime' }, expressions: { working: { eyes: 'wide' } } })
    openExpressions()
    fireEvent.click(screen.getByTestId('avatar-state-reset-working'))
    fireEvent.click(screen.getByTestId('avatar-state-reset-done'))
    apply()
    const saved = lastSaved(onSave) as Picture
    expect(saved.kind).toBe('image')
    expect(saved.v).toBe(2)
    expect(saved.sounds).toBeUndefined()
    expect(saved.expressions).toBeUndefined()
  })

  it('expressions survive a trip through the Picture tab and back', () => {
    const { onSave } = mount()
    openExpressions()
    pick('working', 'eyes', 'squint')
    openPicture()
    openFace()
    apply()
    expect((lastSaved(onSave) as Ghost).expressions).toEqual({ working: { eyes: 'squint' } })
  })

  it('reset to the default face clears the reactions too', () => {
    const { onSave } = mount({ kind: 'ghost', expressions: { working: { eyes: 'wide' } }, sounds: { done: 'chime' } })
    openExpressions()
    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })
})
