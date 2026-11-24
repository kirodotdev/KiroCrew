/**
 * What the XR scene holds and shows: the crew, the pages, the cards, and the test probe.
 *
 * This half has no input in it. `scene.ts` adds the hands, the voice, the keys and the press route on top,
 * and is the only place a write can start from.
 */
import { Camera, Group, Object3D, Ray, Vector2, Vector3, type PerspectiveCamera } from 'three'
import { fan, type Choice, type ChoiceWords, type Fan } from './choices'
import { XR_WRITES_ENABLED as WRITES_BUILT } from './flags'
import type { XrGlance } from './glance'
import type { HandTool } from './hand'
import { BEAD_STEP_MS, beadsDone, leadOf, planOf, type Bead } from './beads'
import type { CancelReason } from './input'
import { assignPlots, type PlotMap } from './land'
import { AURA_VIEW, CARD, CHOICE, DEG, FULL, STRIP, type ViewSpec } from './metrics'
import { replyOf, type XrBot, type XrCrew, type XrNeed, type XrNotice } from './model'
import { Opening, type OpenThing } from './opening'
import { PAGE_MAX, freePlace, pagePos, samePage, type PageKind, type PageRec } from './pages'
import { XR_GL, type XrPalette } from './palette'
import {
  CARD_W, FULL_H, FULL_W, paintBrief, paintBubble, paintCaption, paintCount, paintFull, paintPart, paintShelf, paintStatus, paintStrip, paintTip,
  paintAsk, paintChoices, type XrDraft, type XrThread,
} from './panels'
import { partsOf } from './parts'
import { PointDetector, TalkSession } from './pointTalk'
import type { PressRouter } from './router'
import { applyPatch, applyPatches, sceneFor, type PatchResult, type SceneDoc, type SceneObject, type ScenePatch } from './sceneDoc'
import { Surface, toGl } from './sceneParts'
import type { Drag, Page, Pick, Pointer, XrSceneOptions } from './sceneTypes'
import { XrSound } from './sound'
import type { XrStrings } from './strings'
import { OFFER_AT, OFFER_SCALE, Tokens } from './tokens'
import { TUNE } from './tune'
import { World } from './world'

export const EYE_Y = 1.2
/** The desktop preview looks down this far, through a wide lens, so the desk and the pages are both in the picture. */
export const PREVIEW_LOOK = 24
export const PREVIEW_FOV = 64
export const VIEWS: Record<string, ViewSpec> = { aura: AURA_VIEW }
export const GLANCE_MS = 450
export const LINGER_MS = 700
export const BUBBLE_MS = 8000
export const NOTE_MS = 4500
/** How long handed work takes to travel to the Bot that takes it. */
export const TRAVEL_MS = 500
/** A press becomes a drag only after it moved this far and lasted this long. */
export const DRAG_START = Math.atan(TUNE.holdStartM / FULL.distance)
export const DRAG_MS = 150
/** A turn of the wheel in the preview stands for the two hands; they "let go" this long after the last turn. */
export const WHEEL_REST_MS = 350
/** A page within this angle of the gaze counts as in front of the person. */
export const NEAR_DEG = 45

export const v3 = (x = 0, y = 0, z = 0) => new Vector3(x, y, z)
/**
 * Which part a pointer takes when two are in line: the higher comes first, and within one kind the nearer.
 * The card a Bot holds out, a thing in the air, a page or menu, a marble or the bin, a Bot or a thing of the land, the ground.
 */
export const ORDER = { card: 50, air: 40, surface: 20, token: 10, land: 5, ground: 1 } as const

export const tuple = (v: Vector3): [number, number, number] => [v.x, v.y, v.z]
export const wrap180 = (deg: number): number => ((deg + 540) % 360) - 180

export abstract class XrSceneBase {
  /** The root of the scene's places: the seat and every plain transform in it. None of it is drawn; the components read where each one is. */
  readonly root = new Group()
  /** The preview's camera. The framework owns it; in a session it stands still and the viewer is the head. */
  protected readonly camera: PerspectiveCamera
  /** The person's head in a session: where it is and which way it looks. The page sets it from the viewer pose each frame. */
  readonly viewer = new Camera()
  /** The seat: the person's own space. Its origin is where the head was when the scene began. */
  protected readonly rig = new Group()
  protected readonly ray = new Ray()
  /** The same ray in the seat's space. */
  protected readonly seatRay = new Ray()
  protected readonly head = v3(0, EYE_Y, 0)
  /** The pointer of the desktop preview. It starts off the picture: nothing is pointed at until the mouse comes in. */
  protected readonly mouse = new Vector2(0, 3)
  protected readonly S: XrStrings
  protected readonly sound = new XrSound()
  protected router!: PressRouter
  /** A spoken sentence has a press of its own, by the same rules, so a hand can hold or move a thing while the person talks. */
  protected voice!: PressRouter
  readonly world: World
  /** Writes act only in a build made for them, and never on sample data. */
  protected readonly writesOn: boolean
  /** For the desktop preview: what a pair of glasses shows of the picture. `Marks.tsx` draws its frame. */
  readonly view: ViewSpec | null

  protected crew: XrCrew = { bots: [], needs: [], unowned: 0, feeds: { roster: 'loading', asks: 'loading', live: true } }
  protected notices: readonly XrNotice[] = []
  protected glances = new Map<string, XrGlance>()
  protected threads = new Map<string, XrThread>()
  protected plots: PlotMap
  protected patches: ScenePatch[] = []
  protected pages: Page[] = []
  protected pageSeq = 0
  protected seeded = false
  /** Words that wait for a Bot, by Bot. */
  protected drafts = new Map<string, string>()
  /** The Bot whose draft the keyboard types into, or ''. */
  protected typing = ''

  protected readonly brief: Surface
  protected readonly caption: Surface
  protected readonly bubble: Surface
  protected readonly tip: Surface
  protected readonly shelf: Surface
  protected readonly count: Surface
  protected readonly status: Surface
  protected readonly strip: Surface
  protected readonly chip: Surface
  /** The tray, the bin and the beads: the same in every scene. Where each one is; `Tray.tsx` draws them. */
  readonly tokens = new Tokens()
  /** Where the ring round the thing a pinch would take is, and how big. It shows before the pinch, so a hand can correct its aim. `Marks.tsx` draws it. */
  readonly halo = new Object3D()
  /** Where the mark that travels with handed-over work is. It is shown while it travels. */
  readonly travelling = new Object3D()
  /** The card a Bot holds out: one request, word for word. A finger rests on it or a hand takes it; how that answers is `approval.ts`. */
  protected readonly askCard: Surface
  /** Where the middle of that card is and where it is going, in the seat's space. A hand that holds it moves it. */
  protected readonly cardAt = OFFER_AT.clone()
  protected readonly cardTo = OFFER_AT.clone()
  /** The ray of a hand is on the card, or a hand holds it: its edge lights, before the pinch and for as long as it is held. */
  protected cardLit = false
  /** What is lit right now: the id of the Bot, thing or token, 'card', or ''. It is what a pinch would take. */
  protected litId = ''
  /** Since when the finger has rested on the card or a hand has held it, without a break (0 when neither). */
  protected cardSince = 0
  /** When the card came to rest where its Bot holds it out (0 until then). A yes word counts only a moment after. */
  protected cardRest = 0
  /** The card is in its Bot's hand: a hand has carried it within reach, or the Bot holds it for the beat after a yes. */
  protected cardIn = false
  /** Where the finger on the card (or the hand that holds it) was a frame ago, when, and how fast it moves, in metres a second. */
  protected cardPoint: Vector3 | null = null
  protected cardPointAt = 0
  protected cardSpeed = 0
  /** The first word of the sentence under way: when it started, since when the finger was on the card then, until when it stayed, and the fastest it moved just after. */
  protected word: { at: number; on: number; until: number; fastest: number } | null = null
  /**
   * A yes that waits its beat: the Bot reads the command aloud, and "no", "wait", a palm or taking the card back stops it.
   * `since` and `upAt` are the press the route will judge: when the hand took the card or the finger had rested on it,
   * and when the hand let go (0 for a spoken yes: its press ends with the beat).
   */
  protected pending: { need: string; source: string; since: number; upAt: number; voice: boolean; words: string; at: number } | null = null
  /** Sample data does not speak its scripted line into the sentence under way: it began on the card, or a test speaks it. */
  protected quiet = false
  /** The words of a spoken answer, for the caption of the Bot that was answered: the yes as it was said, or the reason of a no. */
  protected reason: { need: string; text: string } | null = null
  /**
   * The cards a Bot holds out for a thing that was given to it: every card it has, and which turn of "more" is on show.
   * `note` is why the last pinch on a card did not count.
   */
  protected choices: { bot: string; thing: OpenThing; all: Choice[]; page: number; note: string } | null = null
  protected readonly choiceCard: Surface
  /** What would take the thing a hand carries if it let go now: a Bot's slug, 'bin', or ''. That one lights. */
  protected dropLit = ''
  /** The Bot that last lit for a carried thing, and when: a throw goes to it. */
  protected litBot = ''
  protected litAt = 0
  /** The thing a resting finger asked "why" about, which is open for it. */
  protected whyOpen = ''
  /** The hand points right now: index out, hand still. The mouse points while it rests. */
  protected pointingNow = false
  /** What "everyone stop" put away, for "carry on" to bring back. */
  protected halted: { offer: string; choices: XrSceneBase['choices'] } | null = null
  protected stopsAll = 0
  /** A sentence that was understood and starts after one beat, unless "never mind" comes first. */
  protected plan: { bot: string; text: string; about: string; at: number } | null = null
  /** The plans Bots hold up, as beads, and when each began. */
  protected beads = new Map<string, Bead & { at: number; lead: string; scripted: boolean }>()
  /** The title of the thing the finger rested on during the sentence: what "this" means when no hand holds a thing. */
  protected pointed = ''
  /** How long the last sentence waited before it started, in ms: what the tests time the beat by. */
  protected beat = 0
  /** Things thrown into the bin. They can be taken back. */
  protected binned: SceneObject[] = []
  protected airSeq = 0
  /** How many times a Bot was stopped by a palm and thanked by a pat, for the tests. */
  protected stops = 0
  protected pats = 0
  protected said: { slug: string; text: string; until: number } | null = null
  protected token: { from: Vector3; to: Vector3; at: number } | null = null

  /** The Bot under the ray, the object under it, and the Bot the arrow keys chose. */
  protected attend = ''
  protected attendSince = 0
  protected lastOn = 0
  protected tipId = ''
  protected focus = ''
  protected hand = false
  protected shelfOpen = false
  protected knownNeeds: Set<string> | null = null
  protected writes = { sent: 0, approved: 0, rejected: 0, failed: 0, refused: 0 }
  /** How many answers to a card reached the write path, for the tests. On sample data none of them writes. */
  protected answered = { yes: 0, no: 0 }

  /** Point and talk: one sentence at a time, and what became of the last one. */
  protected readonly talk = new TalkSession()
  protected readonly detectors = new Map<string, PointDetector>()
  protected voiceSource = ''
  protected listenAt = 0
  protected spoken: { bot: string; text: string } | null = null
  /** Whether the route has dealt with the sentence that was just handed to it. */
  protected sayDone = false
  protected note: { slug: string; text: string; note: string; until: number } | null = null
  protected ambiguous: string[] = []

  /** What each pointer of the framework is on, and when it last said so: the framework's hover, kept by pointer. */
  protected readonly over = new Map<string, { pick: Pick | null; unsure: string[]; frame: number }>()
  /** Counts the frames. A pointer that said nothing this frame is on nothing. */
  protected frameNo = 0
  /** The pointer whose ray the scene reads right now: the mouse, the head, or a hand or device. */
  protected aiming = 'mouse'
  protected hover: Pick | null = null
  protected drag: Drag | null = null
  /** A press that is down: who presses, on which surface, and which thing two hands could pull apart ('' for none). */
  protected press: { source: string; grab: string; surface: Surface | null; y0: number; yLast: number; scrolling: boolean } | null = null
  protected pan: { source: string; last: Vector3; moved: number } | null = null
  /** Two hands that hold one thing between them: the land, to zoom it, or a thing, to pull it open. */
  protected two: { a: Pointer; b: Pointer; dist: number; open: boolean; page?: Page; scale0: number; track: { at: number; t: number }[] } | null = null
  /** The thing that is pulled open into its parts, a request that was thrown and can still be caught, and how the last throw was read. */
  protected readonly opening: Opening
  protected thrown = ''
  protected flung = 0
  protected about = ''
  /** The plot one zoom heads for. It is chosen when the hands take hold and kept until they let go. */
  protected zoomPlot: string | null = null
  protected wheelAt = 0
  protected held = new Set<string>()
  protected completed = new Set<string>()
  protected keyHeld = ''
  protected inXr = false
  protected placed = false
  protected xrFrames = 0
  protected pointers: Pointer[] = []
  protected active = 'mouse'
  protected lastT = performance.now()
  protected probeAt = 0
  protected disposed = false
  protected readonly cleanup: (() => void)[] = []

  constructor(protected readonly opts: XrSceneOptions) {
    const { palette: pal, icons } = opts
    this.camera = opts.camera
    this.S = opts.strings
    this.plots = opts.plots
    this.writesOn = WRITES_BUILT && !opts.fixture
    const state = { idle: XR_GL.dim, working: toGl(pal.accent), needs: toGl(pal.warn), failed: toGl(pal.danger) }
    this.world = new World({ state }, pal, icons)
    this.root.add(this.rig)
    this.rig.position.set(0, EYE_Y, 0)
    this.rig.add(this.world.land, this.world.seat)
    this.camera.position.copy(this.head)

    const card = (key: string, h: number) => new Surface(key, CARD_W, h, CARD.width, pal, icons, 30)
    this.brief = card('brief', 220)
    this.caption = card('caption', 300)
    this.bubble = card('bubble', 200)
    this.tip = card('tip', 170)
    // The shelf is a menu: it is drawn over a page that stands behind it.
    this.shelf = new Surface('shelf', CARD_W, 580, CARD.width, pal, icons, 80)
    this.count = new Surface('count', 520, 84, 0.22, pal, icons, 8)
    this.status = new Surface('status', 900, 52, 0.42, pal, icons, 7)
    this.strip = new Surface('strip', STRIP.px, 190, STRIP.width, pal, icons, 60)
    this.chip = new Surface('chip', 520, 84, 0.16, pal, icons, 70)
    this.opening = new Opening(pal, icons)
    this.askCard = new Surface('askcard', CARD_W, 520, CARD.width, pal, icons, 85)
    this.askCard.at.scale.setScalar(OFFER_SCALE)
    this.choiceCard = new Surface('choices', CHOICE.px, 480, CHOICE.width, pal, icons, 86)
    // The choice cards are apart from each other: a ray that goes between two of them goes on to what is behind.
    this.choiceCard.gaps = true
    this.halo.visible = false
    this.travelling.visible = false
    this.rig.add(this.askCard.at, this.choiceCard.at, this.halo, this.travelling)
    this.rig.add(this.brief.at, this.caption.at, this.bubble.at, this.tip.at, this.shelf.at, this.count.at, this.status.at, this.chip.at, ...this.opening.cards.map(s => s.at))
    // The count stands past the far edge of the desk, above the land; the build notice ends just left of it.
    this.count.at.position.set(0, -0.3, -1.02)
    this.status.at.position.set(-0.34, -0.295, -0.95)
    // The shelf stands low on the left, beside the desk and under the row the pages open in.
    this.shelf.at.position.set(-0.43, -0.34, -0.51)
    this.count.at.visible = true
    this.status.at.visible = true
    this.view = VIEWS[opts.view] ?? null
    for (const rec of opts.pages) this.addPage({ ...rec })
    this.parkStrip()
  }

  /** What sees the scene right now: the camera in the preview, the person's head in a session. */
  protected get eye(): Camera { return this.inXr ? this.viewer : this.camera }

  /* ---------------- data in ---------------- */



  setCrew(crew: XrCrew, notices: readonly XrNotice[]): void {
    const before = new Map(this.crew.bots.map(b => [b.slug, b]))
    this.crew = crew
    this.notices = notices
    this.world.setCrew(crew.bots, this.glances)
    this.rebuild()
    for (const bot of crew.bots) {
      // What the Bot said when its turn ended is its answer: it says it once, next to itself.
      const was = before.get(bot.slug)
      const reply = was ? replyOf(was, bot) : ''
      if (reply) {
        // A reply that opens with a numbered list is the Bot's plan: it holds it up as beads, and says the line before it.
        const steps = planOf(reply)
        if (steps.length) this.beads.set(bot.slug, { total: steps.length, done: steps.length, at: performance.now(), lead: '', scripted: false })
        this.speak(bot.slug, (steps.length && leadOf(reply)) || reply)
      }
    }
    // A page opened for a Bot that had no thread yet takes the thread once the Bot has one.
    for (const pg of this.pages) if (!pg.rec.slot && pg.rec.slug && pg.rec.kind !== 'hub') pg.rec.slot = this.bot(pg.rec.slug)?.thread ?? ''
    this.noticeNewNeeds()
    this.dirtyAll()
    this.want()
  }

  setGlance(slug: string, g: XrGlance): void {
    this.glances.set(slug, g)
    this.world.setCrew(this.crew.bots, this.glances)
    this.rebuild()
    this.dirtyAll()
  }

  setThread(slot: string, t: XrThread): void {
    this.threads.set(slot, t)
    this.pages.forEach(pg => { if (pg.rec.slot === slot) pg.s.dirty = true })
  }

  /** Make the scene's document again from the data, and lay the person's patches over it. */
  protected rebuild(): void {
    const plots = assignPlots(this.plots, this.crew.bots.map(b => b.slug))
    if (plots !== this.plots) { this.plots = plots; this.opts.onPlots(plots) }
    const base = sceneFor(this.opts.scene)(this.crew.bots, this.glances, plots)
    this.world.setDoc(applyPatches(base, this.patches))
    // A scene may open with pages of its own, when the person has laid out none.
    if (!this.seeded && this.crew.bots.length) {
      this.seeded = true
      if (!this.pages.length) for (const rec of base.pages) this.addPage({ ...rec })
    }
  }

  /** Change the scene's document. The world follows on the next frame; a patch that does not apply changes nothing. */
  patch(p: ScenePatch): PatchResult {
    const r = applyPatch(this.world.doc, p)
    if (r.ok) { this.patches.push(p); this.world.setDoc(r.doc) }
    return r
  }

  get doc(): SceneDoc { return this.world.doc }

  protected bot(slug: string): XrBot | undefined { return this.crew.bots.find(b => b.slug === slug) }
  /** Work that is handed over is seen to go: a small mark travels from where the thing is to the Bot that takes it. */
  protected fly(from: Vector3 | null, to: Vector3 | null): void {
    if (!from || !to) return
    this.travelling.position.copy(from)
    this.travelling.visible = true
    this.token = { from: from.clone(), to: to.clone(), at: performance.now() }
  }

  /** The colours of the page, for the parts that draw. */
  get palette(): XrPalette { return this.opts.palette }

  /** The words on the choice cards, from the language catalog. */
  protected choiceWords(): ChoiceWords {
    const S = this.S
    return { take: S.choiceTake, unblock: S.choiceUnblock, status: S.choiceStatus, summarise: S.choiceSummarise, more: S.choiceMore }
  }

  /** The cards on show now, and how they stand. */
  protected shownFan(): Fan | null { return this.choices ? fan(this.choices.all, this.choices.page, this.choiceWords()) : null }

  /** What a pinch on card `i` would send: that Bot, that thing, that card. A change of any of them disarms the card. */
  protected choiceVersion(i: number): string {
    const c = this.choices
    return c ? `${c.bot}|${c.thing.id}|${c.page}|${i}|${this.shownFan()?.cards[i]?.id ?? ''}` : ''
  }

  /** The request whose card is held out now. */
  protected asked(): XrNeed | undefined { return this.tokens.offer ? this.crew.needs.find(n => n.id === this.tokens.offer) : undefined }
  protected at(slug: string): [number, number, number] { const p = this.world.botTop(slug); return tuple(p ? this.rig.localToWorld(p) : this.head) }

  /** A new ask is heard from its Bot. The Bot hops and its beacon lights; nothing steps in front of the person. */
  protected noticeNewNeeds(): void {
    const ids = new Set(this.crew.needs.map(n => n.id))
    const known = this.knownNeeds
    if (this.crew.feeds.asks !== 'ok' || this.crew.feeds.roster !== 'ok') return
    this.knownNeeds = ids
    if (!known) return
    const fresh = this.crew.needs.find(n => !known.has(n.id) && n.bot)
    if (fresh) this.sound.cue('ask', this.at(fresh.bot))
  }

  /** The Bot says one short line next to itself. It is shown once and fades. */
  protected speak(slug: string, text: string): void {
    if (!this.world.nodes.get(slug)?.body) return
    this.said = { slug, text, until: performance.now() + BUBBLE_MS }
    this.bubble.dirty = true
    this.world.nodes.get(slug)?.body?.speak(true)
    this.sound.cue('reply', this.at(slug))
  }

  /* ---------------- pages ---------------- */

  /** Which way the person faces, as degrees to the right of straight ahead in the seat's space. */
  protected gazeYaw(): number {
    const f = this.eye.getWorldDirection(v3()).applyQuaternion(this.rig.quaternion.clone().invert())
    return Math.atan2(f.x, -f.z) / DEG
  }

  protected addPage(rec: PageRec): Page {
    const s = new Surface(`full:${++this.pageSeq}`, FULL_W, FULL_H, FULL.width, this.opts.palette, this.opts.icons, 40 + this.pageSeq)
    const pg: Page = { s, rec, scale: 1, toast: '', toastUntil: 0, earlier: 1, glideUntil: 0 }
    this.rig.add(s.at)
    // It appears where it belongs. It never flies out from the head.
    s.at.position.set(...pagePos(rec.yaw, rec.pitch))
    s.at.visible = !rec.stored
    this.pages.push(pg)
    return pg
  }

  /** Pull a page out. One that is already out and in front stays where it is; any other comes to where the person looks. */
  protected openPage(kind: PageKind, slug: string, slot = '', topic = ''): Page | null {
    const thread = kind === 'hub' ? '' : slot || this.bot(slug)?.thread || ''
    let pg = this.pages.find(x => samePage(x.rec, { kind, slug, slot: thread }))
    const gaze = this.gazeYaw()
    if (pg) {
      pg.rec.kind = kind
      pg.s.scroll = 0
      pg.toast = ''
      if (pg.rec.stored || Math.abs(wrap180(pg.rec.yaw - gaze)) > NEAR_DEG) {
        const place = freePlace(this.pages.filter(x => x !== pg).map(x => x.rec), gaze)
        if (place) Object.assign(pg.rec, place)
        pg.rec.stored = false
        pg.s.at.visible = true
        pg.s.at.position.set(...pagePos(pg.rec.yaw, pg.rec.pitch))
      }
    } else {
      const place = this.pages.length < PAGE_MAX ? freePlace(this.pages.map(x => x.rec), gaze) : null
      if (!place) { this.say(this.S.pagesFull); return null }
      pg = this.addPage({ kind, slug, slot: thread, topic, ...place, stored: false })
    }
    pg.s.dirty = true
    this.shelf.dirty = true
    this.want()
    this.savePages()
    return pg
  }

  protected storePage(pg: Page): void {
    pg.rec.stored = true
    pg.s.at.visible = false
    if (this.typing === pg.rec.slug) this.typing = ''
    this.shelf.dirty = true
    this.say(this.S.pageStored)
    this.want()
    this.savePages()
  }

  protected closePage(pg: Page | undefined): void {
    if (!pg) return
    pg.s.dispose()
    this.pages = this.pages.filter(x => x !== pg)
    // A draft typed in this page is put down with it. It is kept for that Bot.
    if (this.typing === pg.rec.slug) this.typing = ''
    this.shelf.dirty = true
    this.want()
    this.savePages()
  }

  protected savePages(): void { this.opts.onPages(this.pages.map(pg => ({ ...pg.rec }))) }

  protected openNeed(id: string): void {
    const n = this.crew.needs.find(x => x.id === id)
    if (!n) return
    // The ask opens as the ask: the Bot's dashboard page, where its exact request and command are.
    if (n.bot) this.openPage('dash', n.bot)
    else this.openPage('hub', '')
  }

  /** A marble is picked up. An approval comes out as a card that its Bot holds out. Anything else that waits is read on its page. */
  protected offerNeed(id: string): void {
    const n = this.crew.needs.find(x => x.id === id)
    if (!n) return
    if (n.kind !== 'approval') { this.openNeed(id); return }
    this.tokens.offer = id
    this.askCard.dirty = true
    // The card comes from its Bot to the front of the desk.
    this.cardAt.copy(this.world.botTop(n.bot) ?? OFFER_AT)
    this.cardTo.copy(OFFER_AT)
    this.cardSince = 0
    this.cardRest = 0
    this.cardIn = false
    this.world.nodes.get(n.bot)?.body?.hop()
    this.say(this.S.cardHint)
  }

  /* ---------------- the hand tools ---------------- */

  tool(t: HandTool): void {
    if (t === 'next') {
      // The next thing that waits: its Bot holds out the card, or its page opens. The same as picking its marble up.
      const n = this.crew.needs.find(x => x.id !== this.tokens.offer) ?? this.crew.needs[0]
      if (n) { this.offerNeed(n.id); if (n.bot) { this.focus = n.bot; this.attendTo(n.bot, performance.now()); this.say(this.bot(n.bot)?.label ?? '') } }
    } else if (t === 'shelf') {
      this.shelfOpen = !this.shelfOpen
    } else this.world.whole()
    this.dirtyAll()
  }

  /* ---------------- what a press does ---------------- */

  /** What two hands could take hold of under a pick: a Bot, a thing of the land or a page, as one id. '' for anything else. */
  protected grabId(p: Pick | null): string {
    if (p?.bot || p?.obj) return p.id
    const page = p?.surface && this.pages.find(pg => pg.s === p.surface)
    return page ? `page:${page.s.key}` : ''
  }

  /** The thing behind a grab id: its own title, and the field its parts are read from. */
  protected thingOf(id: string): OpenThing | null {
    const rest = id.slice(id.indexOf(':') + 1)
    if (id.startsWith('bot:')) { const b = this.bot(rest); return b ? { id, bind: { bot: rest, field: 'state' }, label: b.label } : null }
    if (id.startsWith('obj:')) { const d = this.world.objectValue(rest); return d ? { id, bind: d.o.bind, label: d.value.label || d.o.label } : null }
    const rec = this.pages.find(pg => pg.s.key === rest)?.rec
    const bot = rec && this.bot(rec.slug)
    if (!rec || !bot || rec.kind === 'hub') return null
    return { id, bind: { bot: bot.slug, field: rec.slot && rec.slot !== bot.thread ? `thing:${rec.slot}` : 'state' }, label: rec.topic || bot.label }
  }

  /** Where that thing is, in the seat's space. */
  protected centreOf(id: string): Vector3 | null {
    const rest = id.slice(id.indexOf(':') + 1)
    if (id.startsWith('bot:')) return this.world.botTop(rest)
    if (id.startsWith('obj:')) return this.world.objectCentre(rest)
    const pg = this.pages.find(x => x.s.key === rest && !x.rec.stored)
    return pg ? pg.s.at.position.clone().add(v3(0, (pg.s.height * pg.scale) / 2, 0)) : null
  }

  /** Every surface there is now, for the part that draws them: the pages and cards, and the name under each Bot. */
  panels(): Surface[] { return [...this.surfaces(), ...[...this.world.nodes.values()].map(n => n.name)] }

  protected surfaces(): Surface[] {
    return [this.askCard, this.choiceCard, this.brief, this.caption, this.bubble, this.tip, this.shelf, this.count, this.status, this.strip, this.chip, ...this.opening.cards, ...this.pages.map(pg => pg.s)]
  }

  protected attendTo(slug: string, clock: number): void {
    if (slug === this.attend) return
    this.attend = slug
    this.attendSince = clock
    // A Bot answers a hand at once: it nods as it turns to you.
    if (slug) this.world.nodes.get(slug)?.body?.nod()
    this.brief.dirty = true
    this.want()
  }

  /** What a hand holds right now, by its own title: the page or thing being carried, or the thing that is pulled open. '' for nothing. */
  protected holding(): string {
    const d = this.drag
    if (d?.active && d.page) return d.page.rec.topic || this.bot(d.page.rec.slug)?.label || this.S.hub
    if (d?.active && d.label) return d.label
    return this.pointed || (this.opening.thing?.label ?? '')
  }

  /** The fields of a Bot that stand in the air as things pulled out of a page. */
  protected outOf(slug: string): ReadonlySet<string> {
    return new Set(this.world.doc.objects.filter(o => o.id.startsWith('air:') && o.bind?.bot === slug).map(o => o.bind?.field ?? ''))
  }

  /**
   * Light the thing a pinch would take: a Bot, a thing, a marble or the bin. A row of a page lights on the page itself, and the card on its own edge.
   * `at` is its middle in the seat's space and `size` its radius in metres; null hides the ring (nothing to take, or a hand already holds something).
   */
  protected light(at: Vector3 | null, size: number): void {
    this.halo.visible = !!at
    if (!at) return
    // Never thinner than a degree and a half at the eye, so the ring of a tiny Bot in the miniature can be seen.
    const eye = this.rig.worldToLocal(this.head.clone())
    this.halo.position.copy(at)
    this.halo.scale.setScalar(Math.max(size, at.distanceTo(eye) * Math.tan(1.5 * DEG)))
    this.halo.lookAt(this.head)
  }

  /** Put a card above a point of the seat's space, facing the person, big enough to keep its text's angle. */
  protected above(at: Vector3 | null, s: Surface): boolean {
    if (!at) return false
    const world = this.rig.localToWorld(at.clone())
    const scale = Math.max(1, world.distanceTo(this.head) / CARD.distance)
    s.at.scale.setScalar(scale)
    s.at.position.copy(at).add(v3(0, (s.height * scale) / 2 + 0.015 * scale, 0))
    s.at.lookAt(this.head)
    return true
  }

  protected updateSurfaces(clock: number, k: number, dt: number): void {
    const S = this.S
    const now = this.opts.now()
    // The caption: the Bot that listens, or what became of the last sentence.
    if (this.note && clock > this.note.until) this.note = null
    const live = this.talk.phase !== 'idle'
    // The caption shows the live words, then for one beat what was understood, then what became of it.
    const cap = live ? { slug: this.talk.bot, text: this.talk.text, note: '' } : this.plan ? { slug: this.plan.bot, text: this.plan.text, note: S.planBeat } : this.note
    // While the person talks, the caption names what a hand holds or the finger rested on: that is what "this" means in the sentence.
    const about = live ? this.holding() : this.plan?.about ?? ''
    if (about !== this.about) { this.about = about; this.caption.dirty = true }
    const capOn = !!cap && this.above(this.world.botTop(cap.slug), this.caption)
    this.caption.at.visible = capOn
    if (capOn && cap && this.caption.dirty) this.caption.paint(p => paintCaption(p, S, this.bot(cap.slug)?.label ?? '', cap.text, cap.note, about))
    // The thing that is pulled open: its parts, read from the Crew's own fields, stand above it.
    const open = this.opening.thing
    const parts = open?.bind ? partsOf(open.bind, this.bot(open.bind.bot), this.glances.get(open.bind.bot)) : []
    this.opening.update(dt, this.opts.reducedMotion, open ? this.centreOf(open.id) : null, this.rig.worldToLocal(this.head.clone()), this.head, parts, (p, part) => paintPart(p, S, part, now))
    const eye = this.rig.worldToLocal(this.head.clone())
    for (const [slug, b] of this.beads) {
      // Sample data has a scripted Bot: its beads fill one by one. A real plan's beads are filled when its reply is in.
      if (b.scripted) b.done = beadsDone(b.total, clock - b.at, BEAD_STEP_MS)
      if (b.scripted && b.lead && b.done >= b.total) { this.speak(slug, b.lead); b.lead = '' }
      if (clock - b.at > b.total * BEAD_STEP_MS + BUBBLE_MS) this.beads.delete(slug)
    }
    this.tokens.update(clock, this.opts.reducedMotion, this.beads, slug => this.world.botTop(slug), eye)
    // The card a Bot holds out: the one request, word for word.
    const asked = this.asked()
    this.askCard.at.visible = !!asked
    if (asked) {
      this.cardAt.lerp(this.cardTo, k)
      // It has come to rest once it stands where it is held out.
      if (!this.cardRest && this.drag?.kind !== 'card' && this.cardAt.distanceTo(this.cardTo) < 0.004) this.cardRest = clock
      this.askCard.at.position.copy(this.cardAt)
      // In its Bot's hand the card is small, like the Bot.
      const size = OFFER_SCALE * (this.cardIn ? 0.3 : 1)
      this.askCard.at.scale.setScalar(this.askCard.at.scale.x + (size - this.askCard.at.scale.x) * k)
      this.askCard.at.lookAt(this.head)
      if (this.askCard.dirty) {
        let fits = true
        this.askCard.paint(p => { fits = paintAsk(p, S, asked, this.pending ? S.yesBeat : this.opts.voice ? S.cardHint : S.cardHintHands, this.cardLit) })
        // A request that cannot be read whole here cannot be answered here.
        if (!fits) { this.tokens.offer = ''; this.say(S.tooLong); this.openNeed(asked.id) }
      }
    }
    // The cards a Bot holds out for a thing that was given to it stand above it. Nothing else of that Bot is in their way.
    const ch = this.choices
    const shown = this.shownFan()
    const choiceOn = !!ch && !!shown && this.above(this.world.botTop(ch.bot), this.choiceCard)
    this.choiceCard.at.visible = choiceOn
    if (choiceOn && ch && shown && this.choiceCard.dirty) this.choiceCard.paint(p => paintChoices(p, S, ch.thing.label, ch.note, shown, i => this.choiceVersion(i)))
    // The short card: the Bot a finger or a look rests on, once it has rested.
    const g = this.glances.get(this.attend)
    const bot = this.bot(this.attend)
    const briefOn = !!bot && !!g && !(capOn && cap?.slug === this.attend) && clock - this.attendSince > GLANCE_MS && !this.drag?.active && !(choiceOn && ch?.bot === this.attend) && this.above(this.world.botTop(this.attend), this.brief)
    this.brief.at.visible = briefOn
    if (briefOn && bot && g && this.brief.dirty) this.brief.paint(p => paintBrief(p, S, bot, g))
    // What a Bot just said sits above it, unless another card of that Bot is already there.
    if (this.said && clock > this.said.until) { this.world.nodes.get(this.said.slug)?.body?.speak(false); this.said = null }
    const said = this.said
    const bubbleOn = !!said && !(briefOn && this.attend === said.slug) && !(capOn && cap?.slug === said.slug) && !(choiceOn && ch?.bot === said.slug) && this.above(this.world.botTop(said.slug), this.bubble)
    this.bubble.at.visible = bubbleOn
    if (bubbleOn && said && this.bubble.dirty) this.bubble.paint(p => paintBubble(p, said.text))
    // What a thing of the land is, while the ray is on it.
    const thing = this.tipId ? this.world.objectValue(this.tipId) : null
    const tipOn = !!thing && this.above(this.world.objectCentre(this.tipId), this.tip)
    this.tip.at.visible = tipOn
    if (tipOn && thing && this.tip.dirty) this.tip.paint(p => paintTip(p, S, thing.value, thing.o.label, thing.o.more))

    this.count.at.lookAt(this.head)
    if (this.count.dirty) this.count.paint(p => paintCount(p, S, this.crew))
    this.status.at.lookAt(this.head)
    if (this.status.dirty) {
      const f = this.crew.feeds
      const alarm = f.roster === 'failed' || f.asks === 'failed' || !f.live
      const parts = [this.halted ? S.stoppedAll : '', this.ambiguous.length ? S.sayWhich : '', this.writesOn ? '' : S.readOnly, this.opts.fixture ? S.sample : '', f.roster === 'failed' || f.asks === 'failed' ? S.feedFailed : '', f.live ? '' : S.stale].filter(Boolean)
      this.status.paint(p => paintStatus(p, parts, alarm))
    }
    this.shelf.at.visible = this.shelfOpen
    if (this.shelfOpen) {
      this.shelf.at.lookAt(this.head)
      if (this.shelf.dirty) {
        const kinds: Record<PageKind, string> = { chat: S.tabChat, dash: S.tabDash, work: S.tabWork, hub: S.hub }
        const rows = this.pages.map((pg, i) => ({ pg, i })).filter(x => x.pg.rec.stored).slice(0, 6).map(({ pg, i }) => ({
          key: String(i), label: pg.rec.topic || this.bot(pg.rec.slug)?.label || S.hub, kind: kinds[pg.rec.kind], face: this.world.nodes.get(pg.rec.slug)?.thumb ?? null,
        }))
        const things = this.binned.map((o, i) => ({ key: `bin${i}`, label: this.bot(o.bind?.bot ?? '')?.things.find(t => `thing:${t.slot}` === o.bind?.field)?.topic || this.bot(o.zone)?.label || o.id, kind: S.tabWork, face: this.world.nodes.get(o.zone)?.thumb ?? null }))
        this.shelf.paint(p => paintShelf(p, S, [...rows, ...things].slice(0, 6)))
      }
    }
    this.strip.at.visible = this.hand
    if (this.hand && this.strip.dirty) this.strip.paint(p => paintStrip(p, S, this.shelfOpen, this.world.goal.t === 0))
    if (this.chip.at.visible) this.chip.at.lookAt(this.head)
    if (this.token) {
      const t = this.opts.reducedMotion ? 1 : Math.min(1, (clock - this.token.at) / TRAVEL_MS)
      this.travelling.position.lerpVectors(this.token.from, this.token.to, t).add(v3(0, Math.sin(t * Math.PI) * 0.08, 0))
      if (t >= 1) { this.token = null; this.travelling.visible = false }
    }
    for (const pg of this.pages) {
      if (pg.rec.stored) continue
      const s = pg.s
      s.at.position.lerp(v3(...pagePos(pg.rec.yaw, pg.rec.pitch)), clock < pg.glideUntil && k < 1 ? k * 0.35 : k)
      s.at.scale.setScalar(s.at.scale.x + (pg.scale - s.at.scale.x) * k)
      s.at.lookAt(this.head)
      if (pg.toast && clock > pg.toastUntil) { pg.toast = ''; s.dirty = true }
      if (!s.dirty) continue
      const owner = this.bot(pg.rec.slug) ?? null
      const text = owner ? this.drafts.get(owner.slug) : undefined
      const draft: XrDraft | null = owner && pg.rec.kind === 'chat' && (text !== undefined || this.typing === owner.slug) ? { bot: owner.slug, text: text ?? '', typing: this.typing === owner.slug } : null
      const view = {
        kind: pg.rec.kind, bot: owner, face: (owner && this.world.nodes.get(owner.slug)?.thumb) || null, topic: pg.rec.topic, scroll: s.scroll,
        thread: pg.rec.slot ? this.threads.get(pg.rec.slot) ?? null : { lines: [], more: false, state: 'ok' as const },
        glance: (owner && this.glances.get(owner.slug)) || null,
        draft, crew: this.crew, notices: this.notices, toast: pg.toast, now, writes: this.writesOn, out: this.outOf(pg.rec.slug),
      }
      s.paint(p => { s.maxScroll = paintFull(p, S, view) })
      s.scroll = Math.min(s.scroll, s.maxScroll)
    }
  }

  protected dirtyAll(): void { for (const s of this.surfaces()) s.dirty = true }

  /** What the page should read for the scene: every Bot's goals, for its plot, and the threads of the chats that are out. */
  protected want(): void {
    const chats = this.pages.filter(pg => pg.rec.kind === 'chat' && pg.rec.slot && !pg.rec.stored)
    const threads = [...new Set(chats.map(pg => pg.rec.slot))]
    this.opts.onWant({ threads, pages: threads.map(t => Math.max(...chats.filter(pg => pg.rec.slot === t).map(pg => pg.earlier))), bots: this.crew.bots.map(b => b.slug) })
  }

  protected say(text: string): void { if (text) this.opts.onSay(text) }

  /** The person is no longer here on purpose: neither a pinch nor a sentence that was under way may commit. */
  protected away(reason: CancelReason): void { this.router.cancel(reason); this.voice.cancel(reason) }

  /* ---------------- keyboard: every action has a key ---------------- */

  setHand(shown: boolean): void {
    if (this.hand === shown) return
    this.hand = shown
    this.strip.dirty = true
    this.opts.onHand(shown)
  }

  setSound(on: boolean): void { this.sound.enabled = on; this.sound.resume() }

  /* ---------------- desktop preview input ---------------- */

  protected parkStrip(): void {
    this.camera.add(this.strip.at)
    this.strip.at.position.set(-0.24, -0.25, -0.52)
    this.strip.at.rotation.set(-0.25, 0.3, 0)
  }

  /** The picture changed size. The framework has sized the canvas and set the aspect; a narrow window keeps the whole width in view by widening the lens. */
  fit(w: number, h: number): void {
    if (this.inXr) return
    this.camera.fov = w / Math.max(1, h) < 1.2 ? 84 : PREVIEW_FOV
    this.camera.updateProjectionMatrix()
  }

  /* ---------------- the immersive session ---------------- */

  /** Whether a point in the world is inside what the framed glasses show, at the head's pose now. */
  protected seen(w: Vector3): boolean {
    if (!this.view) return true
    const p = this.camera.worldToLocal(w)
    return p.z < 0 && Math.abs(p.x / p.z) <= Math.tan((this.view.h / 2) * DEG) && Math.abs(p.y / p.z) <= Math.tan((this.view.v / 2) * DEG)
  }

  /** What pokes out of the framed view: a Bot counts by its head and feet, a surface by its four corners. */
  protected outside(): string[] {
    const out: string[] = []
    for (const slug of this.world.nodes.keys()) {
      const top = this.world.botTop(slug)
      const mid = this.world.botCentre(slug)
      if (top && mid && !(this.seen(this.rig.localToWorld(top)) && this.seen(this.rig.localToWorld(mid)))) out.push(`bot:${slug}`)
    }
    for (const s of [this.count, ...this.pages.filter(pg => !pg.rec.stored).map(pg => pg.s)]) {
      const corner = ([x, y]: number[]) => this.seen(s.at.localToWorld(v3((x * s.width) / 2, (y * s.height) / 2, 0)))
      if (![[-1, -1], [1, -1], [1, 1], [-1, 1]].every(corner)) out.push(s.key)
    }
    return out
  }

  protected writeProbe(): void {
    const r = this.opts.host.getBoundingClientRect()
    const screen = (w: Vector3): [number, number] => {
      const p = w.clone().project(this.camera)
      return [Math.round(r.left + ((p.x + 1) / 2) * r.width), Math.round(r.top + ((1 - p.y) / 2) * r.height)]
    }
    const world: Record<string, [number, number, number]> = {}
    const targets: Record<string, [number, number]> = {}
    const put = (id: string, seat: Vector3 | null) => {
      if (!seat) return
      const w = this.rig.localToWorld(seat.clone())
      targets[id] = screen(w)
      world[id] = [+w.x.toFixed(3), +w.y.toFixed(3), +w.z.toFixed(3)]
    }
    for (const slug of this.world.nodes.keys()) put(`bot:${slug}`, this.world.botCentre(slug))
    for (const o of this.world.doc.objects) put(`obj:${o.id}`, this.world.objectCentre(o.id))
    for (const id of this.tokens.ids()) put(`tok:${id}`, this.tokens.where(id))
    if (this.asked()) put(`card:${this.tokens.offer}`, this.cardAt)
    // A bare point of the ground, to take hold of: beside the plots, where nothing stands and no Bot walks.
    put('land', v3(this.world.bounds.cx + this.world.bounds.radius * 0.9, 0, this.world.bounds.cz).multiplyScalar(this.world.scale).add(this.world.land.position))
    for (const s of this.surfaces()) {
      if (!s.at.visible) continue
      for (const h of s.hits) {
        const local = v3(((h.x + h.w / 2) / s.canvas.width - 0.5) * s.width, (0.5 - (h.y + h.h / 2) / s.canvas.height) * s.height, 0)
        const w = s.at.localToWorld(local)
        targets[`${s.key}/${h.id}`] = screen(w)
        world[`${s.key}/${h.id}`] = [+w.x.toFixed(3), +w.y.toFixed(3), +w.z.toFixed(3)]
      }
    }
    const { view, goal } = this.world
    this.opts.host.dataset.xrProbe = JSON.stringify({
      scene: this.world.doc.id, writesOn: this.writesOn,
      land: { t: +view.t.toFixed(4), goal: +goal.t.toFixed(4), s: +this.world.scale.toFixed(5), fx: +view.fx.toFixed(3), fz: +view.fz.toFixed(3), y: +this.world.land.position.y.toFixed(3) },
      bots: [...this.world.nodes.values()].map(n => ({ slug: n.bot.slug, state: n.bot.state, ready: !!n.body, pose: n.body?.poseNow ?? '', walking: !!n.body?.walking, needs: n.bot.needs.length, beacon: !!n.body && n.bot.state === 'needs', named: n.name.at.visible })),
      objects: this.world.doc.objects.map(o => { const d = this.world.objectValue(o.id); return { id: o.id, kind: o.kind, zone: o.zone, anchor: o.anchor, found: !!d?.value.found, state: d?.value.state ?? '', level: d?.value.level ?? null, label: d?.value.label ?? '' } }),
      pages: this.pages.map(pg => ({ key: pg.s.key, kind: pg.rec.kind, slug: pg.rec.slug, slot: pg.rec.slot, stored: pg.rec.stored, yaw: +pg.rec.yaw.toFixed(1), pitch: +pg.rec.pitch.toFixed(1), scale: +pg.scale.toFixed(2), scroll: Math.round(pg.s.scroll), maxScroll: Math.round(pg.s.maxScroll), toast: pg.toast, earlier: pg.earlier, lines: this.threads.get(pg.rec.slot)?.lines.length ?? 0 })),
      glances: Object.fromEntries([...this.glances].map(([slug, g]) => [slug, { numbers: g.numbers.length, verdict: !!g.verdict, goals: g.goalsFeed, brief: g.briefFeed }])),
      talk: { phase: this.talk.phase, bot: this.talk.bot, text: this.talk.text }, note: this.note ? { slug: this.note.slug, text: this.note.text, note: this.note.note } : null,
      ambiguous: this.ambiguous, said: this.said ? `${this.said.slug}:${this.said.text}` : '',
      attend: this.attend, focus: this.focus, tip: this.tipId, brief: this.brief.at.visible, hand: this.hand, shelf: this.shelfOpen,
      typing: this.typing, drafts: Object.fromEntries(this.drafts), outside: this.view ? this.outside() : null,
      needs: this.crew.needs.length, feeds: this.crew.feeds, route: this.router.last, voice: this.voice.last, cancel: this.router.guard.lastCancel, writes: this.writes,
      cues: this.sound.log.slice(-6), inXr: this.inXr, active: this.active, sources: this.pointers.map(c => c.id), zooming: !!this.two && !this.two.open, opened: this.opening.probe(), thrown: this.thrown, flung: Math.round(this.flung), holding: this.holding(), drag: this.drag ? [this.drag.kind, this.drag.active, this.drag.pickId ?? ""] : null, offer: this.tokens.offer, onCard: this.cardSince > 0, answered: this.answered, voiceOn: this.opts.voice,
      choices: this.choices ? { bot: this.choices.bot, thing: this.choices.thing.id, cards: this.shownFan()?.cards.map(c => c.id) ?? [], rows: this.shownFan()?.rows ?? [], page: this.choices.page, note: this.choices.note } : null,
      why: this.whyOpen && this.opening.isOpen(this.whyOpen) ? this.whyOpen : '', dropLit: this.dropLit, travelling: !!this.token, halted: !!this.halted, stopsAll: this.stopsAll, rested: this.cardRest > 0, cardIn: this.cardIn, pending: !!this.pending, cardSpeed: +this.cardSpeed.toFixed(3),
      tips: Object.fromEntries(this.pointers.map(c => { const j = c.joints['index-finger-tip'] as Object3D | undefined; const w = j?.visible ? j.getWorldPosition(v3()) : null; return [c.id, w ? [+w.x.toFixed(3), +w.y.toFixed(3), +w.z.toFixed(3)] : null] })),
      plan: this.plan ? { bot: this.plan.bot, text: this.plan.text, about: this.plan.about } : null, beads: Object.fromEntries([...this.beads].map(([k, b]) => [k, [b.done, b.total]])),
      pointed: this.pointed, beat: this.beat, lit: this.halo.visible || this.cardLit || (this.choiceCard.at.visible && !!this.choiceCard.state.hot), litId: this.litId, stops: this.stops, pats: this.pats, binned: this.binned.map(o => o.id), tray: this.tokens.ids().filter(id => id.startsWith('marble:')).length, targets, world,
      head: [+this.head.x.toFixed(3), +this.head.y.toFixed(3), +this.head.z.toFixed(3)],
    })
  }

  dispose(): void {
    this.disposed = true
    this.opts.speech?.stop()
    this.cleanup.forEach(fn => fn())
    for (const s of this.surfaces()) s.dispose()
    this.world.dispose()
    this.sound.dispose()
  }
}
