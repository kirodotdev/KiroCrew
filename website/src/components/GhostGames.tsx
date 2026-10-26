/**
 * Ghost games: the playful variants of the chat "Thinking…" loader.
 *
 * Every scene is the Kiro ghost mark (`assets/ghost-games/ghost.svg`, the mark
 * from `assets/kiro-ghost-mark.svg` with a white body) moving through a row of
 * pellets, or, in `volley`, two of them bumping one pellet back and forth.
 * There is no mouth: the motion does the eating, which reads at the 14px the
 * footer draws a ghost at. The art is an ASSET rendered through
 * `<img>`, the same brand-mark exception GhostPoses relies on: no `<svg>`
 * element or path data lives in TSX. All timing is in chat-transcript.css
 * (`.gg-*`), where each pellet's keyframe is the moment the ghost reaches it,
 * and the ghost's gulp, glow and accent fill-up are keyed to those same bites.
 * The fill-up is a layer masked by a second asset, `ghost-body.svg` (the
 * mark's body alone), so it colours the body and leaves the outline and eyes.
 *
 * Every scene is decorative and aria-hidden: the footer's `role=status` row
 * carries the accessible "Thinking…" name either way.
 */
import ghost from '../assets/ghost-games/ghost.svg'

/** Every game, in the order they were designed. */
export const GHOST_GAMES = [
  'phase', 'pull', 'pop', 'hop', 'inhale', 'collector', 'volley', 'ellipsis',
] as const
export type GhostGame = typeof GHOST_GAMES[number]

/** What the footer shows for one turn: the existing pose carousel or a game. */
export type LoaderChoice = 'carousel' | GhostGame

/** A trailing ellipsis on the localized label, which the `ellipsis` game eats.
 *  Both the single glyph and three ASCII dots occur across the catalogs. */
const TRAILING_ELLIPSIS = /(…|\.\.\.)$/

/** The label with its trailing ellipsis removed, or null when it has none. */
export function labelStem(label: string): string | null {
  return TRAILING_ELLIPSIS.test(label) ? label.replace(TRAILING_ELLIPSIS, '') : null
}

/**
 * Draw one loader for a turn, uniformly over the carousel and every game.
 * `ellipsis` is only eligible when the label ends in an ellipsis to eat; a
 * locale without one would otherwise gain dots its translation never had.
 */
export function pickLoaderChoice(label: string, rand: () => number = Math.random): LoaderChoice {
  const pool: LoaderChoice[] = ['carousel', ...GHOST_GAMES.filter(g => g !== 'ellipsis' || labelStem(label) !== null)]
  return pool[Math.min(pool.length - 1, Math.floor(rand() * pool.length))]
}

/** One ghost: `.gg-ghost` takes the scene's motion, `.gg-body` the gulp and
 *  glow on each bite, and `.gg-tint` (masked to the body by ghost-body.svg in
 *  the CSS) fills it with the accent colour a step per bite. */
function Ghost() {
  return (
    <span className="gg-ghost">
      <span className="gg-body">
        <img className="gg-art" src={ghost} alt="" aria-hidden="true" draggable={false} />
        <span className="gg-tint" />
      </span>
    </span>
  )
}

/** `count` pellets sharing one base class; `.gg-pN` picks each one's slot. */
function Pellets({ count, className = 'gg-dot' }: { count: number; className?: string }) {
  return <>{Array.from({ length: count }, (_, i) => <span key={i} className={`${className} gg-p${i + 1}`} />)}</>
}

/** One ghost travelling over four pellets: the shape every single-ghost game shares. */
function Run() {
  return <><Pellets count={4} /><span className="gg-mover"><Ghost /></span></>
}

/** One game scene. For `ellipsis` the footer renders the label stem right before
 *  it, so the crumbs the ghost pulls in read as the label's own "…". */
export function GhostGameLoader({ game }: { game: GhostGame }) {
  const scene = (() => {
    switch (game) {
      case 'phase':
      case 'pull':
      case 'pop':
      case 'hop':
      case 'inhale':
        return <Run />
      case 'collector':
        return <span className="gg-scene"><Run /></span>
      case 'volley':
        // Two ghosts bump one ball back and forth; the right one is mirrored.
        return <>
          <span className="gg-player gg-left"><Ghost /></span>
          <span className="gg-player gg-right"><Ghost /></span>
          <span className="gg-ball"><span className="gg-dot" /></span>
        </>
      case 'ellipsis':
        return <><Pellets count={3} className="gg-crumb" /><Ghost /></>
    }
  })()
  return (
    <span
      className={`gg gg-${game}${game === 'ellipsis' ? ' text-muted' : ''}`}
      data-testid="loader-game"
      data-game={game}
      aria-hidden="true"
    >{scene}</span>
  )
}
