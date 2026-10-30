import type { MutableRefObject } from 'react'
import { motion, useReducedMotion } from 'framer-motion'

import { i18nT } from '../i18n/t'
import { ICON_BG_PALETTE, POSE_IDS, poseDataUri } from '../lib/avatarPoses'
import { BRAND_PURPLE } from '../lib/kiroGhostAvatar'

/**
 * The "Meet your Mates." launch intro, in the style of the launch poster.
 *
 * This is the body of the startup intro dialog for the `crewmates` catalog
 * entry (`component: "crewmates"` on the offer): a rendered component, not a
 * clip, so nothing is fetched and nothing can 404. `StartupVideoModal` owns the
 * dialog around it -- scrim, focus trap, verdict, "Not now" / "Try now" -- and
 * this file owns only what the poster shows: the title, one line, a sample chat
 * card from a Mate, and two rows of crewmate faces drifting past in opposite
 * directions.
 *
 * Every colour is the active theme's: the dialog is the theme's card with a
 * quiet wash of its accent, the outlines are the accent mixed into the theme's
 * border, and the text is the theme's own, so the intro reads the same way in
 * a light theme and a dark one. Only the crewmate faces keep their fixed
 * palette: each face is a crewmate's avatar colour, not chrome.
 */

/** Faces per row. Twice `POSE_IDS` so every pose shows up in each row with a
 *  different colour, and the row is long enough to loop on a 640px card. */
const FACES_PER_ROW = 11
/** Seconds for one full pass of a row; the rows loop seamlessly. */
const ROW_LOOP_S = 18

/** `n` faces, poses in order from `offset`, each over a different palette colour. */
function faces(n: number, offset: number) {
  return Array.from({ length: n }, (_, k) => {
    const i = k + offset
    return (
      <img
        key={i}
        alt=""
        // The poses are a fixed set of SVG files recoloured as data URIs, the
        // same way the roster draws an icon avatar.
        src={poseDataUri(POSE_IDS[i % POSE_IDS.length], ICON_BG_PALETTE[i % ICON_BG_PALETTE.length])}
        className="w-[78px] h-[78px] rounded-full shrink-0 shadow-md"
      />
    )
  })
}

/**
 * One row of faces sliding past. The content is laid twice end to end and the
 * row travels exactly one copy's width, so the loop has no seam. Under
 * reduce-motion the row stands still on its first copy.
 */
function FaceRow({ top, offset, reverse, reduceMotion }: { top: number; offset: number; reverse: boolean; reduceMotion: boolean }) {
  return (
    <motion.div
      className="absolute left-0 flex gap-[18px] w-max"
      style={{ top }}
      initial={{ x: reverse ? '-50%' : '0%' }}
      animate={reduceMotion ? undefined : { x: reverse ? ['-50%', '0%'] : ['0%', '-50%'] }}
      transition={{ duration: ROW_LOOP_S, ease: 'linear', repeat: Infinity }}
      data-testid={reverse ? 'mates-intro-row-right' : 'mates-intro-row-left'}
    >
      {faces(FACES_PER_ROW, offset)}
      {faces(FACES_PER_ROW, offset)}
    </motion.div>
  )
}

export interface MeetYourMatesIntroProps {
  /** `id` for the heading, for the dialog's `aria-labelledby`. */
  titleId: string
  /** `id` for the line under it, for the dialog's `aria-describedby`. */
  descId: string
  /** The catalog's title and line, already translated by the caller. */
  title: string
  description: string
  onNotNow: () => void
  onTryNow: () => void
  /** The dialog focuses "Try now" on open, so the ring does not make "Not now"
   *  read as the main choice. */
  tryNowRef: MutableRefObject<HTMLButtonElement | null>
  /** Mate's avatar in the sample card: where "Not now"'s flight to the rail
   *  starts. The dialog measures it at click time. */
  avatarRef: MutableRefObject<HTMLImageElement | null>
}

export default function MeetYourMatesIntro({ titleId, descId, title, description, onNotNow, onTryNow, tryNowRef, avatarRef }: MeetYourMatesIntroProps) {
  const reduceMotion = useReducedMotion() ?? false
  const pillBase = 'rounded-full px-3 py-1 text-[13px] font-medium'
  // The sample card and the two outlined buttons share one outline: the accent
  // mixed into the theme's border.
  return (
    // Surface: the theme's card washed with its accent. Frame: a stronger
    // accent mix, so the dialog stands off a scrim of the same tone.
    <div
      data-testid="meet-your-mates-intro"
      className="flex flex-col rounded-[18px] text-text-strong"
      style={{ background: 'color-mix(in srgb, var(--accent) 8%, var(--card))', border: '1px solid color-mix(in srgb, var(--accent) 30%, var(--border))' }}
    >
      <div className="px-9 pt-8 pb-2">
        <h2 id={titleId} className="m-0 mb-2.5 text-[40px] leading-[1.05] font-semibold tracking-[-0.02em]">{title}</h2>
        <p id={descId} className="m-0 text-[17px] text-muted">{description}</p>
      </div>

      {/* A sample of what a Mate's message looks like: the sender, two short
          lines, and the two choices it offers. Static, not a real thread, so the
          pills are text and not buttons. */}
      <div className="mx-9 mt-4 rounded-[14px] px-4 py-3.5 bg-card" style={{ border: '1.5px solid color-mix(in srgb, var(--accent) 45%, var(--border))' }}>
        <div className="flex items-center gap-2 text-[15px] font-bold">
          {/* Mate's avatar. On "Not now" this is what flies to the Mates rail
              row and becomes its "New" tag, so the dialog hands it up by ref. */}
          <img ref={avatarRef} alt="" src={poseDataUri(POSE_IDS[0], BRAND_PURPLE)} className="w-7 h-7 rounded-full" data-testid="mates-intro-avatar" />
          {i18nT('components.assistantWelcome.default_name')}
        </div>
        <div className="ml-9 mt-2 flex flex-col items-start gap-1.5">
          <span className="rounded-[10px] px-2.5 py-1.5 text-sm bg-accent-subtle">
            {i18nT('components.meetYourMatesIntro.sample_line_1')}
          </span>
          <span className="rounded-[10px] px-2.5 py-1.5 text-sm bg-accent-subtle">
            {i18nT('components.meetYourMatesIntro.sample_line_2')}
          </span>
        </div>
        <div className="ml-9 mt-2.5 flex gap-2">
          <span className={`${pillBase} bg-accent text-accent-fg`}>{i18nT('components.meetYourMatesIntro.sample_merge')}</span>
          <span className={pillBase} style={{ border: '1.5px solid color-mix(in srgb, var(--accent) 45%, var(--border))' }}>{i18nT('components.meetYourMatesIntro.sample_show_first')}</span>
        </div>
      </div>

      {/* Decoration: the crew drifting past, top row leftward, bottom row
          rightward. Nothing in it is named, so it is hidden from the tree. The
          rows run off the card's edges with a hard clip, as on the poster: no
          fade over them. */}
      <div className="relative mt-1.5 h-[190px] overflow-hidden" aria-hidden="true">
        <FaceRow top={8} offset={0} reverse={false} reduceMotion={reduceMotion} />
        <FaceRow top={102} offset={5} reverse reduceMotion={reduceMotion} />
      </div>

      <div className="flex justify-end gap-2.5 px-6 pt-2.5 pb-5.5">
        <button
          type="button"
          data-testid="startup-video-not-now"
          className="rounded-full px-5 py-2.5 text-[15px] font-semibold bg-transparent text-text-strong cursor-pointer"
          style={{ border: '1.5px solid color-mix(in srgb, var(--accent) 45%, var(--border))' }}
          onClick={onNotNow}
        >
          {i18nT('components.startupVideoModal.not_now')}
        </button>
        <button
          ref={tryNowRef}
          type="button"
          data-testid="startup-video-try-it"
          className="rounded-full px-5 py-2.5 text-[15px] font-semibold bg-accent text-accent-fg border-none hover:opacity-90 cursor-pointer"
          onClick={onTryNow}
        >
          {i18nT('components.startupVideoModal.try_now')}
        </button>
      </div>
    </div>
  )
}
