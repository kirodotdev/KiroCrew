import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { render } from '@testing-library/react'
import { GHOST_GAMES, GhostGameLoader, labelStem, pickLoaderChoice } from '../components/GhostGames'

const ROOT = resolve(__dirname, '..')
/** chat-transcript.css with comments stripped, so prose quoting a rule is not read as one. */
const CSS = readFileSync(join(ROOT, 'styles/chat-transcript.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
const asset = (name: string) => readFileSync(join(ROOT, 'assets/ghost-games', name), 'utf8')

/** Each single-ghost scene's loop length and the bites (percent of the loop) the ghost reacts to. */
const SCENES: Record<string, { seconds: number; bites: number[] }> = {
  phase: { seconds: 2.1, bites: [26.5, 47.5, 69, 86.5] },
  pull: { seconds: 2.1, bites: [19.3, 42, 63.3, 86] },
  pop: { seconds: 2.1, bites: [26, 47.3, 68.7, 86] },
  hop: { seconds: 2.4, bites: [16, 40, 62, 84] },
  inhale: { seconds: 1.5, bites: [0, 25, 50, 75] },
  collector: { seconds: 2.4, bites: [19.3, 36.7, 54, 71.3] },
  ellipsis: { seconds: 2.25, bites: [29.5, 44.5, 59.5] },
}

/** Every ghost that reacts, by its keyframe suffix: its touches and the fill after each. */
const REACTORS: Record<string, { bites: number[]; fills: string[] }> = {
  ...Object.fromEntries(Object.entries(SCENES).map(([s, { bites }]) =>
    [s, { bites, fills: bites.length === 3 ? ['.32', '.64', '.95'] : ['.25', '.5', '.75', '.95'] }])),
  // Volley: four crossings a loop; the left ghost touches at 0% and 50%, the
  // right one at 25% and 75%, each about half full per touch.
  'volley-l': { bites: [0, 50], fills: ['.475', '.95'] },
  'volley-r': { bites: [25, 75], fills: ['.475', '.95'] },
}

/** The body of `@keyframes name`, as `{offsets: string[], decl}` blocks. */
function keyframes(name: string) {
  const m = CSS.match(new RegExp(`@keyframes ${name}\\{((?:[^{}]*\\{[^}]*\\})*)\\}`))
  if (!m) throw new Error(`no @keyframes ${name}`)
  return [...m[1].matchAll(/([^{}]+)\{([^}]*)\}/g)].map(b => ({ offsets: b[1].split(',').map(s => parseFloat(s)), decl: b[2] }))
}
/** The declaration value a keyframe sets at exactly `pct`, if it names that offset. */
const at = (name: string, pct: number) => keyframes(name).find(b => b.offsets.includes(pct))?.decl

/** Every `animation:` duration set by rules whose selector starts with `.gg-<scene> `. */
function sceneDurations(scene: string) {
  const out: number[] = []
  for (const m of CSS.matchAll(new RegExp(`\\.gg-${scene}[ >][^{]*\\{[^}]*animation:([^;}]+)`, 'g'))) {
    for (const part of m[1].split(',')) out.push(parseFloat(part.trim().split(/\s+/)[1]))
  }
  return out
}

describe('labelStem', () => {
  it('drops a trailing ellipsis glyph or three ASCII dots', () => {
    expect(labelStem('Thinking…')).toBe('Thinking')
    expect(labelStem('Réflexion...')).toBe('Réflexion')
  })

  it('returns null when there is no trailing ellipsis to eat', () => {
    expect(labelStem('思考中')).toBeNull()
    expect(labelStem('Thinking… now')).toBeNull()
  })
})

describe('pickLoaderChoice', () => {
  /** Every value the draw can land on, sweeping the random source across [0, 1). */
  const reachable = (label: string) => {
    const seen = new Set<string>()
    for (let i = 0; i < 100; i++) seen.add(pickLoaderChoice(label, () => i / 100))
    return seen
  }

  it('can land on the carousel and on every game', () => {
    expect(reachable('Thinking…')).toEqual(new Set(['carousel', ...GHOST_GAMES]))
  })

  it('never draws the ellipsis game for a label without an ellipsis', () => {
    const seen = reachable('思考中')
    expect(seen.has('ellipsis')).toBe(false)
    expect(seen.size).toBe(GHOST_GAMES.length)
  })

  it('stays in range at the top of the random source', () => {
    expect(pickLoaderChoice('Thinking…', () => 0.9999999)).toBe(GHOST_GAMES[GHOST_GAMES.length - 1])
  })
})

describe('GhostGameLoader', () => {
  it.each(GHOST_GAMES)('%s is decorative brand art: aria-hidden <img> assets, no inline svg', game => {
    const { container, getByTestId } = render(<GhostGameLoader game={game} />)
    const root = getByTestId('loader-game')
    expect(root).toHaveAttribute('data-game', game)
    expect(root).toHaveAttribute('aria-hidden', 'true')
    expect(container.querySelector('svg')).toBeNull()
    const imgs = container.querySelectorAll('img')
    expect(imgs.length).toBeGreaterThan(0)
    imgs.forEach(img => {
      expect(img).toHaveAttribute('alt', '')
      expect(img.getAttribute('src')).toBeTruthy()
    })
  })
})


describe('GhostGameLoader ghosts', () => {
  it.each(GHOST_GAMES)('%s: every ghost is motion > body > (art img + tint layer)', game => {
    const { container } = render(<GhostGameLoader game={game} />)
    const ghosts = container.querySelectorAll('.gg-ghost')
    expect(ghosts.length).toBe(game === 'volley' ? 2 : 1)
    ghosts.forEach(g => {
      const body = g.querySelector(':scope > .gg-body')
      expect(body).not.toBeNull()
      expect(body!.querySelectorAll(':scope > img.gg-art')).toHaveLength(1)
      expect(body!.querySelectorAll(':scope > .gg-tint')).toHaveLength(1)
    })
  })

  it('volley is two ghosts facing each other over one ball, with no net or pellet row', () => {
    const { container } = render(<GhostGameLoader game="volley" />)
    const root = container.querySelector('.gg-volley')!
    expect(root.querySelectorAll(':scope > .gg-player.gg-left > .gg-ghost')).toHaveLength(1)
    expect(root.querySelectorAll(':scope > .gg-player.gg-right > .gg-ghost')).toHaveLength(1)
    expect(root.querySelectorAll(':scope > .gg-ball > .gg-dot')).toHaveLength(1)
    expect(root.querySelectorAll('.gg-dot')).toHaveLength(1)
    expect(root.children).toHaveLength(3)
    // The left ghost faces right, as the mark does; the right one is mirrored.
    expect(CSS).toMatch(/\.gg-right\{[^}]*transform:scaleX\(-1\)/)
    expect(CSS).not.toMatch(/\.gg-left\{[^}]*scaleX/)
  })

  it('the tint mask is the mark body alone, in the same frame as the mark', () => {
    const body = asset('ghost-body.svg')
    const mark = asset('ghost.svg')
    const viewBox = (s: string) => s.match(/viewBox="([^"]+)"/)?.[1]
    expect(viewBox(body)).toBe(viewBox(mark))
    expect(body.match(/<path /g)).toHaveLength(1)
    expect(body).not.toMatch(/stroke/)
    // The body path is the mark's first path, so the mask lines up with the art.
    const d = (s: string) => s.match(/<path d="([^"]+)"/)?.[1]
    expect(d(body)).toBe(d(mark))
    expect(CSS).toMatch(/\.gg-tint\{[^}]*mask:url\('\.\.\/assets\/ghost-games\/ghost-body\.svg'\)/)
    expect(CSS).toMatch(/\.gg-tint\{[^}]*background:var\(--accent\)[^}]*mix-blend-mode:multiply/)
  })
})

describe('ghost game timing (chat-transcript.css)', () => {
  it.each(Object.keys(SCENES).filter(s => s !== 'inhale'))('%s runs every loop on one shared duration', scene => {
    const durations = sceneDurations(scene)
    expect(durations.length).toBeGreaterThan(0)
    expect(new Set(durations)).toEqual(new Set([SCENES[scene].seconds]))
  })

  it('inhale keeps its bites on a 1.5s loop with pellets every .375s', () => {
    expect(CSS).toMatch(/\.gg-inhale \.gg-dot\{[^}]*animation:gg-flow 1\.5s /)
    expect(CSS).toMatch(/\.gg-inhale \.gg-p2\{animation-delay:\.375s\}/)
    expect(CSS).toMatch(/\.gg-inhale \.gg-p3\{animation-delay:\.75s\}/)
    expect(CSS).toMatch(/\.gg-inhale \.gg-p4\{animation-delay:1\.125s\}/)
    // The first pellet lands one loop in, so the bite loops wait one loop.
    expect(CSS).toMatch(/\.gg-inhale \.gg-body\{animation:gg-gulp-inhale 1\.5s linear 1\.5s infinite,gg-full-inhale 1\.5s linear 1\.5s infinite\}/)
    expect(CSS).toMatch(/\.gg-inhale \.gg-tint\{animation:gg-fill-inhale 1\.5s linear 1\.5s infinite\}/)
  })

  it('volley runs every loop on 2.7s, with four crossings and the ball never eaten', () => {
    const durations: number[] = []
    for (const m of CSS.matchAll(/(?:\.gg-(?:volley|left|right) [^{]*|\.gg-ball)\{[^}]*animation:([^;}]+)/g)) {
      for (const part of m[1].split(',')) durations.push(parseFloat(part.trim().split(/\s+/)[1]))
    }
    // Ball x and y, then per ghost: hop, gulp, glow and fill.
    expect(durations.length).toBe(10)
    expect(new Set(durations)).toEqual(new Set([2.7]))
    // The ball is at the left head at 0/50/100% and the right head at 25/75%...
    expect(at('gg-serve', 0)).toBe('transform:translateX(0)')
    expect(at('gg-serve', 25)).toBe('transform:translateX(73px)')
    expect(at('gg-serve', 50)).toBe('transform:translateX(0)')
    expect(at('gg-serve', 75)).toBe('transform:translateX(73px)')
    // ...arcing up between every touch and back down onto each head.
    for (const p of [0, 25, 50, 75, 100]) expect(at('gg-arc', p)).toMatch(/^transform:translateY\(0\)/)
    for (const p of [12.5, 37.5, 62.5, 87.5]) expect(at('gg-arc', p)).toMatch(/^transform:translateY\(-11px\)/)
    // No pellet ever disappears: the ball has no opacity keyframe.
    expect(keyframes('gg-serve').concat(keyframes('gg-arc')).some(k => k.decl.includes('opacity'))).toBe(false)
  })

  it('volley draws its taller art upward without growing the footer row', () => {
    // A 38px box less 16px of top margin and 4px of bottom margin is the 18px
    // every other scene takes.
    expect(CSS).toMatch(/\.gg-volley\{[^}]*height:38px;margin:-16px 0 -4px/)
    expect(CSS).toMatch(/\.gg\{[^}]*height:18px/)
    // The track edges fade like the other scenes.
    expect(CSS).toMatch(/\.gg-volley\{[^}]*mask-image:linear-gradient\(90deg,transparent,#000 18px/)
  })

  it.each(Object.keys(REACTORS))('%s: the ghost gulps, fills up and finally glows on its own bites', scene => {
    const { bites, fills } = REACTORS[scene]
    bites.forEach((b, i) => {
      // A gulp starts from rest at each bite...
      expect(at(`gg-gulp-${scene}`, b)).toBe('transform:scale(1)')
      // ...and the fill steps up from the previous level there.
      expect(at(`gg-fill-${scene}`, b)).toBe(`opacity:${i === 0 ? 0 : fills[i - 1]}`)
    })
    const squashes = keyframes(`gg-gulp-${scene}`).find(k => k.decl === 'transform:scale(1.12,.9)')
    expect(squashes?.offsets).toHaveLength(bites.length)
    expect(keyframes(`gg-fill-${scene}`).some(k => k.decl === `opacity:${fills[fills.length - 1]}`)).toBe(true)
    // The glow is dark until the final bite.
    expect(at(`gg-full-${scene}`, bites[bites.length - 1])).toBe('filter:drop-shadow(0 0 0 transparent)')
    expect(keyframes(`gg-full-${scene}`).filter(k => k.decl.includes('var(--accent)'))).toHaveLength(1)
  })

  it.each(['inhale', 'volley-l', 'volley-r', 'ellipsis'])('%s fades its tint out before the loop wraps, where the ghost is still in view', scene => {
    expect(at(`gg-fill-${scene}`, 100)).toBe('opacity:0')
  })

  it('the label shimmer keeps its slow 2.8s sweep', () => {
    expect(CSS).toMatch(/\.thinking-shimmer\{[^}]*animation:thinking-shimmer 2\.8s linear infinite/)
  })
})
