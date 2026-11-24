/**
 * Every number a gesture is read by, in one table, so that a tuning pass
 * lands in one place.
 *
 * Where they come from: two feel labs, and the amendments to the grammar
 * that settle where the two disagree. Every number was found with scripted
 * hands and simulated noise. None comes from a person's hands, and none has
 * been measured on a device.
 */
export type TwoHandRule = 'window' | 'double'

export const TUNE = {
  /** A pinch that moves this far, at the distance of what it is on, is a hold: a row pops free, a page comes along. Less is a press. */
  holdStartM: 0.08,
  /** A row a hand must pinch is at least this high. */
  rowMinM: 0.034,
  /** A pinch must have been open this long before it counts as let go: a tracker drops a pinch for a frame or two. */
  pinchOpenMs: 70,
  /**
   * How two hands on one thing are measured. The labs disagree, so both rules are kept, and `?twohand=` picks one on a device.
   * `window`: a fixed window of the gap: shut under `gapCloseM`, open at `gapOpenM`, wherever the hands took hold.
   * `double`: the gap against the gap when they took hold: twice the gap is open, and the midpoint of the two hands is what takes hold.
   */
  twoHandRule: 'window' as TwoHandRule,
  gapOpenM: 0.1,
  gapCloseM: 0.055,
  /** With `double`: the land's scale goes with the gap ratio to this power. */
  zoomGain: 3.6,
  /** A zoom that is let go while it still moves this fast carries on to the end. In whole zooms a second, over the last `zoomFlingMs`. */
  zoomFlingPerS: 0.64,
  zoomFlingMs: 150,
  /** A thing let go faster than this flies. Slower, it is put down where the hand lets go. Speeds are of the thing itself, in metres a second. */
  throwFlyMps: 0.7,
  /** The card a Bot holds out must have rested this long before a yes word can count. */
  cardRestMs: 600,
  /** A finger is on the card when its tip is inside the card's outline and within this of its face. */
  cardTouchM: 0.025,
  /** A yes word counts when the finger is on the card as the word starts, is still on it this much later, and was never faster than `cardStillMps` in between. */
  cardStayMs: 350,
  /** A finger on the card counts only while it moves slower than this. A card is in its Bot's hand only when it is let go slower than this. */
  cardStillMps: 0.15,
  /**
   * A card that is handed back ends in its Bot's hand when it is let go within this of the Bot, or within half the Bot's
   * height when that is more. The lab gives the rule and no distance: this is about the size of a Bot in the miniature.
   */
  handBackM: 0.03,
  /** After a yes the Bot reads the command aloud and waits this long. "No" or "wait" in that time stops it. */
  yesBeatMs: 600,
  /** A point that rests on a thing this long, with the hand still, asks why: the thing opens with its part in trouble lit. A tap or a passing hand does not. */
  whyHoldMs: 400,
  /** Choice cards stand two or three in one row, and four to eight in rows of up to `choiceRowMax`. Past `choiceMax` the last card is "more". */
  choiceRowMax: 4,
  choiceMax: 8,
  /** A choice card is at least this wide and this high when it is `choiceAtM` away, so its title is one degree tall. */
  choiceCardWM: 0.105,
  choiceCardHM: 0.065,
  choiceAtM: 0.6,
  /** A thing that is thrown goes to a Bot when that Bot was lit this short a time before the hand let go. Otherwise it goes to the bin. */
  throwAimMs: 400,
  /**
   * Both palms pushed forward, and held there this long: everyone stop. Both hands then pulled back this far: carry on.
   * The lab gives the time and no distance. The push is as far as the pull back: two open hands that are only held up push nothing.
   */
  bothPalmsMs: 350,
  palmsPushM: 0.06,
  pullBackM: 0.06,
  /** Work that is carried to be given is drawn this small, above the fingertip, so the Bot under it stays in sight. */
  carryScale: 0.4,
  /** Parked with the key (`approval.ts`): it clicks at this much wrist turn, either way. The picture of the turn ends at a quarter turn. */
  keyTurnDeg: 68,
  /** The key is out of its lock when the hand has moved this far, or its ray has swung this far, with the key still straight. */
  keyPullM: 0.04,
  keyPullDeg: 8,
  /** Quiet after words that ends a sentence: after a complete sentence, and after one that is still open. */
  sayPauseMs: 600,
  sayPauseOpenMs: 1200,
  /**
   * How long a Bot shows what it understood before a sentence leaves the room. A sent message cannot be taken back,
   * so it waits long enough to say "never mind". (A change to the thing in the hand would start at once, with the old
   * version beside it as the undo; the page has no such change yet.)
   */
  sayBeatMs: 900,
} as const
