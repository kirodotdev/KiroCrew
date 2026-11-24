/**
 * The XR scene: the interaction layer of `/xr`, and nothing else.
 *
 * It owns four things, and they are the same in every scene:
 *
 *   1. The land's zoom. Two hands pulled apart zoom in toward the plot the
 *      person looks at, in one move from a miniature on the desk to full
 *      size; one hand drags the ground (`land.ts`).
 *   2. Point and talk. A finger that rests on a Bot makes it listen; a pause
 *      or a dropped hand ends the sentence (`pointTalk.ts`).
 *   3. Pages. The dashboard's own surfaces, pulled out and placed around the
 *      desk, put away and found again (`pages.ts`).
 *   4. The press route. Every pinch, key and spoken sentence becomes an
 *      action in `router.ts`, and a write happens only there.
 *
 * What the world holds is not decided here. A scene document says it
 * (`sceneDoc.ts`), `World` keeps where each thing stands (`world.ts`), the
 * components draw it (`Land.tsx`, `Bot.tsx`, `Panels.tsx`, `Tray.tsx`,
 * `Marks.tsx`), and a patch to the document changes the world on the next
 * frame.
 *
 * On a browser with no headset the same scene runs as a preview: the mouse
 * is the finger ray, a click is a pinch, the wheel is the two-hand pull, and
 * a drag on the ground is the one-hand drag.
 */
import { Object3D, Ray, type Group, type Vector2, type Vector3 } from 'three'
import { HANDS, WAYS, answerOf, onCardForWord, stopsYes, tipOnCard, whereLet } from './approval'
import { leadOf, planOf } from './beads'
import { choicesFor } from './choices'
import { tryWrite } from './flags'
import { BothHands, Dwell, PALM, PatDetector, palmOver, palmToward, type OpenHand } from './gestures'
import { HAND_TOOLS, palmView, stripShown, type HandTool, type V3 } from './hand'
import { flick, isAway, landing, litOf, speedOf, takerOf, throwKind } from './handling'
import { AIM_REST_MS } from './input'
import { botHeight, handZoom, zoomFling } from './land'
import { DEG, FULL } from './metrics'
import type { XrNeed } from './model'
import { placeOf, type PageKind } from './pages'
import { hitAt } from './painter'
import { POINT, PointDetector, chooseBot, endsWith, fingerShape, type HandSample, type Joints, type TalkEvent } from './pointTalk'
import { PressRouter, type Refusal, type RouteTarget } from './router'
import {
  DRAG_MS, DRAG_START, EYE_Y, LINGER_MS, NOTE_MS, ORDER, PREVIEW_LOOK, WHEEL_REST_MS, XrSceneBase, tuple, v3, wrap180,
} from './sceneBase'
import { applyPatch, type ScenePatch } from './sceneDoc'
import type { Surface } from './sceneParts'
import type { Drag, Page, Pick, Pointer, XrSceneOptions } from './sceneTypes'
import { sourceIdFor, type XrSourceLike } from './sources'
import { BIN_AT, MARBLE_R, OFFER_AT, OFFER_SCALE, TOKEN_PICK_DEG } from './tokens'
import { OBJECT_PICK_DEG } from './world'
import { TUNE } from './tune'

export type { XrThread } from './panels'
export { BUBBLE_MS } from './sceneBase'
export type { XrSceneOptions, XrSpeech, XrWant } from './sceneTypes'

/** A thing in the air stands this far along the hand's ray, and is this big: about an orange at half an arm. */
const AIR = { distance: 0.5, scale: 0.09 }

export class XrScene extends XrSceneBase {
  private readonly palms = new Map<string, Dwell>()
  private readonly patting = new Map<string, PatDetector>()
  private trayKey = '?'
  private readonly whyDwell = new Dwell(TUNE.whyHoldMs)
  private whyOn = ''
  /** The thing a hand has just pinched: its resting finger does not ask why of it until it has pointed elsewhere. */
  private whyDone = ''
  /** How far the carried card was from its Bot's hand in the last frame the hand held it, in metres. */
  private cardBotM: number | null = null
  private readonly both = new BothHands()
  constructor(opts: XrSceneOptions) {
    super(opts)
    const hooks = {
      act: (id: string) => this.act(id),
      write: (id, version) => this.write(id, version),
      refused: (id: string, why: Refusal) => this.refused(id, why),
    } satisfies ConstructorParameters<typeof PressRouter>[0]
    this.router = new PressRouter(hooks)
    this.voice = new PressRouter(hooks)
    this.listen()
  }

  private act(id: string): void {
    this.sound.resume()
    // A short pinch on a solid thing or a Bot does nothing: a thing is taken by holding it, and a Bot is talked to.
    if (id.startsWith('obj:') || id.startsWith('bot:')) return
    // A marble is picked up: its Bot holds out the card, or its page opens. The bin shows what is in it.
    if (id.startsWith('tok:')) { if (id === 'tok:bin') this.shelfOpen = !this.shelfOpen; else this.offerNeed(id.slice(11)); this.dirtyAll(); return }
    const [scope, hit = ''] = id.split('/')
    const [verb, ...rest] = hit.split(':')
    const arg = rest.join(':')
    const page = this.pages.find(pg => pg.s.key === scope) ?? null
    const slug = page?.rec.slug ?? ''
    switch (verb) {
      case 'bot': this.openPage('chat', arg); break
      case 'need': this.openNeed(arg); break
      case 'offer': this.offerNeed(arg); break
      case 'thing': this.openPage('chat', slug, arg, this.bot(slug)?.things.find(t => t.slot === arg)?.topic ?? ''); break
      case 'hub': case 'grip': this.openPage('hub', ''); break
      case 'tool': if ((HAND_TOOLS as readonly string[]).includes(arg)) this.tool(arg as HandTool); break
      case 'page': {
        // A row of the bin: a page that was put away comes back to where the person looks, a thing to where it was.
        const pg = this.pages[Number(arg)]
        const thing = arg.startsWith('bin') ? this.binned.splice(Number(arg.slice(3)), 1)[0] : undefined
        if (thing) this.patch({ op: 'add', object: thing })
        else if (pg) this.openPage(pg.rec.kind, pg.rec.slug, pg.rec.slot, pg.rec.topic)
        break
      }
      case 'close': this.closePage(page ?? undefined); break
      case 'store': if (page) this.storePage(page); break
      case 'tab': if (page) this.openPage(arg as PageKind, page.rec.slug, page.rec.slot, page.rec.topic); break
      case 'earlier': if (page) { page.earlier += 1; this.want() } break
      case 'kbd': if (page) this.typing = this.typing === page.rec.slug ? '' : page.rec.slug; break
      case 'more': if (this.choices) { this.choices.page += 1; this.choices.note = '' } break
      case 'leave': this.opts.onLeave(hit.slice(6)); break
      default: break
    }
    this.dirtyAll()
  }

  /** A pinch on a thing of the land pulls out the page behind it. */
  private pull(id: string): Page | null {
    const d = this.world.objectValue(id)
    if (!d?.o.page) return null
    const slug = d.o.bind?.bot || d.o.zone
    const topic = d.o.page.kind === 'chat' && d.o.page.slot !== this.bot(slug)?.thread ? d.value.label : ''
    return this.openPage(d.o.page.kind, slug, d.o.page.slot, topic)
  }

  private toast(scope: string, msg: string): void {
    const page = this.pages.find(pg => pg.s.key === scope)
    if (page) { page.toast = msg; page.toastUntil = performance.now() + 4000; page.s.dirty = true }
    // An answer has no page to show on: its Bot's caption says what became of it, under the words it was given in.
    const answered = /^(yes|no):/.test(scope) ? this.crew.needs.find(n => n.id === scope.slice(scope.indexOf(':') + 1)) : undefined
    if (answered) { this.note = { slug: answered.bot, text: this.reason?.need === answered.id ? this.reason.text : '', note: msg, until: performance.now() + NOTE_MS }; this.caption.dirty = true }
    this.say(msg)
  }

  /** What a spoken sentence to a Bot would send: that Bot, in its own thread. */
  private sayVersion(slug: string): string { return `${slug}|${this.bot(slug)?.thread ?? ''}` }

  /** Words that were not sent are kept for that Bot, and the caption says why. */
  private keep(slug: string, text: string, note: string): void {
    if (text) this.drafts.set(slug, [this.drafts.get(slug), text].filter(Boolean).join(' ').slice(0, 2000))
    this.note = { slug, text, note, until: performance.now() + NOTE_MS }
    this.caption.dirty = true
    if (note) this.say(note)
  }

  /** A guarded press that the router committed, on the request it began on. The only caller of `tryWrite`. */
  private write(id: string, version: string): void {
    // An answer to a request is a yes or a no and nothing else, named by the request, whole. How it was said is not known here.
    const answer = /^(yes|no):/.exec(id)?.[1] ?? ''
    const asked = answer ? id.slice(answer.length + 1) : ''
    const [scope, hit = ''] = asked ? [id] : id.split('/')
    const [verb, ...rest] = hit.split(':')
    const arg = rest.join(':')
    const S = this.S
    const page = this.pages.find(pg => pg.s.key === scope)
    const spoken = scope.startsWith('say:') ? scope.slice(4) : ''
    if (spoken) this.sayDone = true
    // A choice card that was pinched is a sentence to its Bot, said by hand: the card's own words, about the thing that was given.
    const chosen = scope === 'choices' && verb === 'choose' ? this.choices : null
    const card = chosen ? this.shownFan()?.cards[Number(arg)] : undefined
    // A sentence with no page of its own: spoken, or chosen. What becomes of it shows on its Bot.
    const said = spoken || chosen?.bot || ''
    if (said || verb === 'send') {
      const slug = said || page?.rec.slug || ''
      const bot = this.bot(slug)
      const text = (chosen ? (card && card.id !== 'more' ? `${card.label}\n${S.aboutThis(chosen.thing.label)}` : '') : spoken ? (this.spoken?.bot === slug ? this.spoken.text : '') : this.drafts.get(slug) ?? '').trim()
      const want = chosen ? this.choiceVersion(Number(arg)) : spoken ? this.sayVersion(slug) : `${slug}|${this.drafts.get(slug) ?? ''}`
      if (!bot || !text || want !== version) { this.refused(id, 'changed'); return }
      const where = this.at(slug)
      void tryWrite({ kind: 'send', bot: slug, slot: bot.thread, text }, this.writesOn).then(r => {
        if (this.disposed) return
        if (r.ok) {
          this.writes.sent += 1
          this.sound.cue('commit', where)
          this.world.nodes.get(slug)?.body?.setPose('think')
          if (!said) { this.drafts.delete(slug); if (this.typing === slug) this.typing = '' }
          if (said) this.keep(slug, '', S.sent)
          else this.toast(scope, S.sent)
          this.opts.onWrote()
        } else {
          const msg = r.reason === 'disabled' ? S.sendDisabled : S.writeFailed
          this.failed(scope, msg, where, r.reason)
          // Words that did not go out are not lost: they wait in that Bot's chat.
          if (said) this.keep(slug, text, msg)
        }
        // The Bot puts its cards away once one was taken.
        if (chosen && this.choices === chosen) this.choices = null
        this.dirtyAll()
      })
      return
    }
    const need = this.crew.needs.find(n => n.id === (asked || arg))
    const kind = answer === 'yes' ? 'approve' : answer ? 'reject' : verb
    if (answer === 'yes' || answer === 'no') this.answered[answer] += 1
    if ((kind !== 'approve' && kind !== 'reject') || !need || need.version !== version) { this.refused(id, 'changed'); return }
    const where = asked ? this.at(need.bot) : tuple(page?.s.at.getWorldPosition(v3()) ?? this.head)
    void tryWrite({ kind, need }, this.writesOn).then(r => {
      if (this.disposed) return
      if (r.ok) {
        this.writes[kind === 'approve' ? 'approved' : 'rejected'] += 1
        this.sound.cue('commit', where)
        this.toast(scope, kind === 'approve' ? S.approved : S.rejected)
        this.opts.onWrote()
      } else this.failed(scope, r.reason === 'disabled' ? S.writesDisabled : S.writeFailed, where, r.reason)
      this.dirtyAll()
    })
  }

  private failed(scope: string, msg: string, where: [number, number, number], reason: string): void {
    if (reason === 'failed') this.writes.failed += 1
    this.sound.cue('refuse', where)
    this.toast(scope, msg)
  }

  /** A guarded press that did not count. It never writes; it says why, so the press can be learned. */
  private refused(id: string, why: Refusal): void {
    const S = this.S
    this.writes.refused += 1
    // An answer that did not count changes nothing: the request still waits, and its Bot's caption says why.
    const answer = /^(yes|no):/.exec(id)?.[1]
    if (answer) {
      const quick = why === 'too-short' || why === 'no-aim' || why === 'disarmed'
      this.toast(id, why === 'puck' ? S.deviceHint : quick ? S.cardAgain : answer === 'yes' && !this.writesOn ? S.writesDisabled : answer === 'no' ? S.refuseKept : S.changedHint)
      return
    }
    const scope = id.split('/')[0]
    const send = scope.startsWith('say:') || id.includes('/send') || scope === 'choices'
    const msg = !this.writesOn ? (send ? S.sendDisabled : S.writesDisabled)
      : why === 'too-short' ? S.holdHint : why === 'no-aim' ? S.aimHint : why === 'disarmed' ? S.waitHint : why === 'puck' ? S.deviceHint : why === 'changed' || why === 'spent' ? S.changedHint : ''
    // A choice card that did not count keeps its cards out, and says why above them.
    if (scope === 'choices') { if (this.choices && msg) { this.choices.note = msg; this.choiceCard.dirty = true; this.say(msg) } return }
    // A spoken sentence that was refused is kept, never dropped.
    if (scope.startsWith('say:')) { this.sayDone = true; this.keep(scope.slice(4), this.spoken?.bot === scope.slice(4) ? this.spoken.text : '', msg || S.sayKept) } else if (msg) this.toast(scope, msg)
  }

  /* ---------------- pointing ---------------- */

  /**
   * A pointer of the framework is on a part of the scene. `who` is the mouse, the head, or a hand or device of the
   * session. `obj` is what its ray met first, `point` where, `uv` where on a surface, and `ray` the ray itself.
   */
  meets(who: 'mouse' | 'head' | XrSourceLike, obj: Object3D, point: Vector3, uv: Vector2 | undefined, ray: Ray): void {
    const source = typeof who === 'string' ? who : this.pointers.find(c => c.source === who)?.id
    if (!source) return
    this.over.set(source, { ...this.pickOf(obj, point, uv, ray), frame: this.frameNo })
  }

  /** What a part of the scene is to a hand: a place on a surface, the card, a token, a thing, a Bot, or the ground. */
  private pickOf(obj: Object3D, point: Vector3, uv: Vector2 | undefined, ray: Ray): { pick: Pick | null; unsure: string[] } {
    const none = { pick: null, unsure: [] }
    const s = obj.userData.surface as Surface | undefined
    if (s === this.askCard) return { pick: { id: `card:${this.tokens.offer}`, tok: 'card', point }, unsure: [] }
    if (s) {
      if (!uv) return none
      const px = s.px(uv.x, uv.y)
      const hit = hitAt(s.hits, px[0], px[1])
      return { pick: { id: `${s.key}/${hit?.id ?? ''}`, surface: s, hit, px, point }, unsure: [] }
    }
    const id = (obj.userData.pick as string | undefined) ?? ''
    if (id.startsWith('tok:')) return { pick: { id, tok: id.slice(4), point }, unsure: [] }
    if (id.startsWith('obj:')) return { pick: { id, obj: id.slice(4), point }, unsure: [] }
    if (id.startsWith('bot:')) {
      // The ray is on a Bot. When two Bots stand close, which of them it means is the pointing rule's to say:
      // the one that already listens keeps its turn, and two that cannot be told apart are neither.
      const origin = this.rig.worldToLocal(ray.origin.clone())
      const direction = ray.direction.clone().applyQuaternion(this.rig.quaternion.clone().invert())
      const chosen = chooseBot(this.world.candidates(origin, direction), this.talk.bot)
      return { pick: chosen.id ? { id: `bot:${chosen.id}`, bot: chosen.id, point } : null, unsure: chosen.ambiguous }
    }
    if (id === 'land') {
      const seat = this.rig.worldToLocal(point.clone())
      return { pick: { id, ground: this.world.groundOf(seat), point }, unsure: [] }
    }
    return none
  }

  /**
   * What the pointer that aims is on, as the framework's hover says. `skip` is a surface a hand carries: it is not in
   * its own way.
   */
  private pick(skip?: Surface): Pick | null {
    const o = this.over.get(this.aiming)
    // A pointer the framework has not heard from since the frame before this one is on nothing. (A press is told
    // to the scene before the frame's own look at where the pointer is, so the look of the frame before counts.)
    if (!o || o.frame < this.frameNo - 1) return null
    if ((o.unsure.length > 0) !== (this.ambiguous.length > 0)) this.status.dirty = true
    this.ambiguous = o.unsure
    const p = o.pick
    if (skip && p && (p.surface === skip || (skip === this.askCard && p.tok === 'card'))) return null
    return p
  }

  /** What a pick is on the press route. The card that is held out is a yes to exactly one request, and the only solid thing a guarded press can end on. */
  private routeId(p: Pick | null): string { return p?.tok === 'card' ? `yes:${this.tokens.offer}` : p?.id ?? '' }

  private targetOf(p: Pick | null): RouteTarget | null {
    if (!p || !(p.hit || p.bot || p.obj || p.ground || p.tok)) return null
    const asked = p.tok === 'card' ? this.asked() : undefined
    return asked ? { id: this.routeId(p), guarded: true, version: asked.version } : { id: p.id, guarded: p.hit?.guarded, version: p.hit?.version }
  }

  private down(source: string): void {
    const clock = performance.now()
    this.active = source
    this.sound.resume()
    this.held.add(source)
    const p = this.pick()
    // The hand that points ends its own sentence when it pinches. The other hand may pinch, hold and carry while the person talks.
    // A pinch on the card ends nothing: the hand that takes it is still on that request.
    if (this.voiceSource === `${source}~voice` && p?.tok !== 'card') {
      const talking = this.talk.stop()
      if (talking) this.onTalk(talking, '', clock)
    }
    this.completed.delete(source)
    this.router.down(source, this.targetOf(p), clock)
    this.press = { source, grab: this.grabId(p), surface: p?.surface ?? null, y0: p?.px?.[1] ?? 0, yLast: p?.px?.[1] ?? 0, scrolling: false }
    // A pinch on the ground takes hold of the land.
    this.pan = p?.ground ? { source, last: p.ground.seat.clone(), moved: 0 } : null
    const base = { source, dir: this.ray.direction.clone(), at: clock, active: false, samples: [] as Drag['samples'] }
    // A pinch on the card takes hold of it. It is a guarded press from here on, and what it means is read when the hand lets go.
    if (p?.tok === 'card') {
      // Taking the card back out of the Bot's hand stops a yes that still waits its beat.
      this.cancelPending()
      this.drag = { ...base, kind: 'card', pickId: p.id, startDist: this.cardAt.distanceTo(this.seatRay.origin) }
      return
    }
    if (p?.bot || p?.obj) {
      // A Bot that is pinched answers at once, before the hand has moved.
      if (p.bot) this.world.nodes.get(p.bot)?.body?.hop()
      // A hand that takes hold is no longer asking why: what the resting finger opened shuts, and that finger asks again only once it has pointed elsewhere.
      if (this.whyOpen) { if (this.opening.isOpen(this.whyOpen)) this.opening.shut(); this.whyOpen = '' }
      this.whyDone = p.obj ? `obj:${p.obj}` : ''
      // A pinch that moves is a hold. A thing in the air comes with the hand; a Bot or a thing of the land gives up its page.
      this.drag = { ...base, kind: p.obj?.startsWith('air:') ? 'carry' : 'pull', pickId: p.id, label: p.obj ? this.thingOf(p.id)?.label : undefined }
      return
    }
    const kind = p?.hit?.drag
    if (!kind || !p?.surface) return
    const page = this.pages.find(pg => pg.s === p.surface)
    this.drag = {
      ...base, kind, page,
      startScale: page?.scale, startDist: page ? p.point.distanceTo(page.s.at.getWorldPosition(v3())) : 0,
      row: kind === 'thing' && page ? { bot: page.rec.slug, field: p.hit?.id ?? '' } : undefined,
    }
  }

  /**
   * `late` is true when the pinch opened a moment ago and only now counts as let go: the hand's way since is not part of a throw.
   * `at` is when the hand let go, when that was before now.
   */
  private up(source: string, completed: boolean, late = false, at?: number): void {
    const clock = performance.now()
    const d = this.drag
    const pan = this.pan
    this.drag = null
    this.press = null
    this.pan = null
    this.held.delete(source)
    // What was lit to take the thing, as of the last moment the hand held it.
    const lit = this.dropLit
    this.dropLit = ''
    if (pan?.moved) this.world.release()
    // What is behind the thing that was in the hand: a page or a card that is carried is not in its own way.
    const p = this.pick(d?.kind === 'card' ? this.askCard : d?.active && d.kind === 'move' ? d.page?.s : undefined)
    // Where the hand points as it lets go is the last of what a throw is read from.
    if (d?.active && !late) d.samples.push({ t: at ?? clock, ...placeOf(this.seatRay.direction.x, this.seatRay.direction.y, this.seatRay.direction.z) })
    if (d?.kind === 'card' && d.source === source) { this.cardUp(d, p, source, completed, clock, late); return }
    if (d?.active) {
      this.router.cancel('scrolled')
      this.router.up(source, '', clock, false)
      // A drag ends in something only when the same source finished its press. A cancelled one changes nothing.
      if (completed && d.source === source) {
        const f = flick(d.samples)
        // A page is at reading distance and a thing in the air nearer: the same turn of the wrist moves the page faster.
        const thrown = throwKind(f.speed, d.kind === 'carry' ? AIR.distance : FULL.distance)
        this.flung = f.speed
        // What was lit as the hand let go takes the work: the bin, or the Bot. Thrown, it goes to the Bot that lit a moment before.
        const taker = !d.gift || p?.tok === 'bin' || lit === 'bin' ? '' : takerOf(thrown, lit, this.litBot, clock - this.litAt)
        if (taker && d.gift && this.give(taker, d.gift)) {
          // What was carried is a view of the work. The Bot has what it stands for: a page that came out for this goes back in,
          // and one that was already out goes back to where it was.
          if (d.page && d.fresh) this.closePage(d.page)
          else if (d.page && d.home) { d.page.scale = d.startScale ?? 1; Object.assign(d.page.rec, d.home) }
          else if (d.pickId && d.homePos) this.nudge(d.pickId.slice(4), d.homePos)
          this.thrown = thrown
          if (d.page) this.savePages()
        } else if (d.kind === 'move' && d.page) {
          // Work that was carried small and not given is a page again, hung from its header where the hand let go.
          if (d.gift) { d.page.scale = d.startScale ?? 1; Object.assign(d.page.rec, this.pagePlace(d.page, false)) }
          if (p?.surface === this.strip || p?.surface === this.shelf || p?.tok === 'bin') this.storePage(d.page)
          else if (thrown === 'throw') {
            // A thrown page glides on and stays where it lands. Thrown off the workbench, it is put away.
            const to = landing(d.page.rec, f)
            if (isAway(to)) this.storePage(d.page)
            else Object.assign(d.page.rec, { yaw: wrap180(to.yaw), pitch: to.pitch })
          }
          if (thrown === 'throw') d.page.glideUntil = clock + 1200
          this.thrown = thrown
          this.savePages()
        } else if (d.kind === 'carry' && d.pickId) {
          // A thing that is put down stays where the hand let go. Thrown, or let go over the bin, it lands in the bin.
          this.thrown = thrown
          if (thrown === 'throw' || p?.tok === 'bin') this.toBin(d.pickId.slice(4))
        }
      }
      this.dirtyAll()
      return
    }
    this.router.up(source, p?.id ?? '', clock, completed)
  }

  /** The card was let go. What that means is read by `approval.ts`; a yes or a no then goes through the press route. */
  private cardUp(d: Drag, p: Pick | null, source: string, completed: boolean, clock: number, late: boolean): void {
    const n = this.asked()
    const f = flick(d.samples)
    const dist = d.startDist ?? AIR.distance
    const thrown = d.active ? throwKind(f.speed, dist) : 'place'
    // The card is in its Bot's hand only when it ended there, within reach and at rest. A move toward the Bot is not that.
    // A hand's pinch counts as open a moment after it opened: where the card was is read from before that moment, as its speed is.
    const at = { bin: p?.tok === 'bin', botM: late ? this.cardBotM : n ? this.cardToBot(n) : null, reachM: this.handReach(), speedMps: speedOf(f.speed, dist) }
    // Only a card that the hand carried and let go itself can be an answer. One that was only pinched, or that the system took, is none.
    const answer = n && d.active && completed ? answerOf({ onCard: true, words: '', letGo: whereLet(thrown, at) }, this.S, this.opts.voice ? WAYS : HANDS) : 'nothing'
    if (d.active) { this.thrown = thrown; this.flung = f.speed }
    this.reason = null
    this.cardIn = false
    if (answer === 'yes' && n && this.opts.voice && this.router.pressedId === `yes:${n.id}`) {
      // With voice: the pinch that took the card is the guarded press, and it is still good. The Bot takes the card and reads
      // the command: the yes goes to the route after that beat, as a press from the pinch to this release.
      this.router.drop(source)
      this.cardIn = true
      this.cardTo.copy(this.world.botHand(n.bot, this.rig.worldToLocal(this.head.clone())) ?? OFFER_AT)
      this.confirm(n, source, d.at, clock, false, '', clock)
    } else if (answer === 'yes' && n) {
      // With hands only the yes goes at once: the pinch that took the card is the guarded press, and it ends here.
      // The route still refuses a press that was cancelled on the way, was too short, or began without a rested aim.
      this.router.up(source, `yes:${n.id}`, clock, true)
      this.cardTo.copy(OFFER_AT)
    } else {
      this.router.drop(source)
      // A refused card goes back to where it is held out, for as long as its request waits. One that was put down stays where the hand let go.
      if (answer === 'no' && n) { this.answer(n.id, 'no', source, d.at, clock, this.router); this.cardTo.copy(OFFER_AT) } else if (d.active) {
        // Put down, it stays where the fingertip let go of it.
        this.cardTo.copy(this.seatRay.at(dist, v3()))
        this.say(this.S.cardPut)
      }
    }
    this.dirtyAll()
  }

  /** How far the card is from its Bot's hand: from the Bot's middle to the ray the card is carried on, in metres. */
  private cardToBot(n: XrNeed): number | null {
    const hand = this.world.botCentre(n.bot)
    return hand ? this.seatRay.distanceToPoint(hand) : null
  }

  /** How near its Bot a card must be let go to end in the Bot's hand. A Bot that is drawn big has a big hand. */
  private handReach(): number { return Math.max(TUNE.handBackM, botHeight(this.world.scale) * 0.5) }

  /** The Bot whose middle is nearest the ray and within reach of it, or ''. */
  private botInReach(): string {
    let best = ''
    let bestM = this.handReach()
    for (const b of this.crew.bots) {
      const c = this.world.botCentre(b.slug)
      const m = c ? this.seatRay.distanceToPoint(c) : Infinity
      if (m <= bestM) { best = b.slug; bestM = m }
    }
    return best
  }

  /**
   * Give work to a Bot. The work is seen to travel to it, and the Bot holds out what it can do about it, as cards.
   * Only work can be given: a Bot's own page is not a thing.
   */
  private give(slug: string, what: string): boolean {
    const thing = this.thingOf(what)
    const b = this.bot(slug)
    const field = thing?.bind?.field ?? ''
    if (!thing?.bind || !b || !(field.startsWith('thing:') || field.startsWith('goal:'))) return false
    const state = this.bot(thing.bind.bot)?.things.find(t => `thing:${t.slot}` === field)?.state ?? ''
    this.choices = { bot: slug, thing, all: choicesFor({ owner: thing.bind.bot, state }, slug, this.choiceWords()), page: 0, note: '' }
    this.fly(this.centreOf(what), this.world.botTop(slug))
    this.world.nodes.get(slug)?.body?.hop()
    this.sound.cue('handoff', this.at(slug))
    this.say(`${b.label}. ${this.choices.all.map(c => c.label).join('. ')}`)
    this.dirtyAll()
    return true
  }

  /**
   * Both palms: "Everyone stop." Every Bot puts away what it holds out, and a sentence or a yes under way is dropped.
   * Nothing is refused, and "carry on" brings it back.
   * shortcut: Crew has no call that pauses every Bot from this page, so this stops what the page holds out, not the Bots' work.
   */
  private stopAll(clock: number): void {
    this.halted = { offer: this.tokens.offer, choices: this.choices }
    this.cancelPending()
    if (!this.cancelPlan()) { const ev = this.talk.drop(); if (ev) this.onTalk(ev, '', clock) }
    if (this.drag?.kind === 'card') { this.router.drop(this.drag.source); this.drag = null }
    this.tokens.offer = ''
    this.choices = null
    for (const b of this.crew.bots) this.world.nodes.get(b.slug)?.body?.setPose('stand')
    this.stopsAll += 1
    this.note = null
    this.sound.cue('refuse', tuple(this.head))
    this.say(this.S.stoppedAll)
    this.dirtyAll()
  }

  /** Both hands pulled back: "Carry on." What "everyone stop" put away is held out again. */
  private carryOn(): void {
    const h = this.halted
    if (!h) return
    this.halted = null
    if (h.offer) this.offerNeed(h.offer)
    if (h.choices && this.bot(h.choices.bot)) this.choices = h.choices
    this.sound.cue('arrive', tuple(this.head))
    this.say(this.S.carryOn)
    this.dirtyAll()
  }

  /**
   * A yes was given. It does not go yet: the Bot reads the command aloud and waits one beat, in which "no", "wait",
   * a palm or taking the card back stops it. Then the yes goes to the press route, as the press described by `since` and `upAt`.
   */
  private confirm(n: XrNeed, source: string, since: number, upAt: number, voice: boolean, words: string, clock: number): void {
    this.pending = { need: n.id, source, since, upAt, voice, words, at: clock }
    // shortcut: the command is shown beside the Bot and not spoken, so the beat runs from the yes; with a voice it runs from the end of the reading.
    this.note = null
    this.speak(n.bot, n.command)
    this.opts.speech?.start()
    this.askCard.dirty = true
    this.say(`${n.command} ${this.S.yesBeat}`)
  }

  /** The beat after a yes is over: the yes goes to the press route, which still checks the hold, the still card and the unchanged request. */
  private updatePending(clock: number): void {
    const p = this.pending
    if (!p || clock - p.at < TUNE.yesBeatMs) return
    this.endPending()
    this.reason = p.words ? { need: p.need, text: p.words } : null
    this.answer(p.need, 'yes', p.source, p.since, p.upAt || clock, p.voice ? this.voice : this.router)
    this.dirtyAll()
  }

  /** The beat after a yes ends, one way or the other: the Bot lets go of the card and no longer listens for a no. */
  private endPending(): void {
    this.pending = null
    this.cardIn = false
    this.cardTo.copy(OFFER_AT)
    this.askCard.dirty = true
    if (this.talk.phase === 'idle' && !this.plan) this.opts.speech?.stop()
  }

  /** Stop a yes that still waits its beat. Nothing was answered, and the request waits as before. */
  private cancelPending(): boolean {
    const p = this.pending
    if (!p) return false
    const n = this.crew.needs.find(x => x.id === p.need)
    this.endPending()
    if (n) this.keep(n.bot, '', this.S.stopped)
    return true
  }

  /**
   * A yes or a no to one request, as a guarded press of its own on `via`. `since` is when the hand took the card, the finger
   * had rested on it, or the sentence ended. So an answer needs the same hold, the same still card and the same unchanged
   * request as every other guarded press, and a device that cannot approve cannot answer.
   */
  private answer(needId: string, answer: 'yes' | 'no', source: string, since: number, clock: number, via: PressRouter): void {
    const n = this.crew.needs.find(x => x.id === needId)
    if (!n) return
    const id = `${answer}:${n.id}`
    via.hover(`${source}~${answer}`, id, since - AIM_REST_MS)
    via.down(`${source}~${answer}`, { id, guarded: true, version: n.version }, since)
    via.up(`${source}~${answer}`, id, clock, true)
  }

  /** What two hands hold between them. */
  private midPick(a: Pointer, b: Pointer): Pick | null {
    // shortcut: the middle of two rays has no pointer of its own. When both hands are on the same thing it is that thing;
    // else it is what the second hand is on. Give the middle a pointer of the framework if `twohand=double` is kept.
    const pa = this.over.get(a.id)?.pick ?? null
    const pb = this.over.get(b.id)?.pick ?? null
    return pa && pb && pa.id === pb.id ? pa : pb
  }

  /** A row pulled out of a page becomes a thing in the air: one object of the scene, bound to the same field, so it stays live. */
  private pullRow(row: { bot: string; field: string }): string {
    const had = this.world.doc.objects.find(o => o.id.startsWith('air:') && o.bind?.bot === row.bot && o.bind.field === row.field)
    if (had) return had.id
    const id = `air:${++this.airSeq}`
    const goal = row.field.startsWith('goal:')
    const slot = row.field.startsWith('thing:') ? row.field.slice(6) : this.bot(row.bot)?.thread ?? ''
    const r = this.patch({ op: 'add', object: { id, kind: goal ? 'goal' : 'work', anchor: 'seat', zone: row.bot, pos: tuple(this.seatRay.at(AIR.distance, v3())), scale: AIR.scale, bind: row, page: { kind: goal ? 'dash' : 'chat', slot } } })
    if (!r.ok) return ''
    this.sound.cue('handoff', tuple(this.head))
    this.say(this.S.pulledOut(this.thingOf(`obj:${id}`)?.label ?? ''))
    return id
  }

  /** Move a thing of the scene to where the hand is. Many small moves of one thing are one patch, not a growing list. */
  private nudge(id: string, pos: [number, number, number]): void {
    const p: ScenePatch = { op: 'set', id, pos }
    const r = applyPatch(this.world.doc, p)
    if (!r.ok) return
    const last = this.patches.at(-1)
    if (last?.op === 'set' && last.id === id) this.patches[this.patches.length - 1] = p
    else this.patches.push(p)
    this.world.setDoc(r.doc)
  }

  /** A thrown thing lands in the bin. It is a view: nothing of the Crew's is deleted, and it can be taken back. */
  private toBin(id: string): void {
    const o = this.world.doc.objects.find(x => x.id === id)
    if (!o || !this.patch({ op: 'remove', id }).ok) return
    this.binned = [...this.binned, o].slice(-6)
    if (this.opening.thing?.id === `obj:${id}`) this.opening.shut()
    this.sound.cue('refuse', tuple(this.rig.localToWorld(BIN_AT.clone())))
    this.say(this.S.binned)
  }

  private updateHover(clock: number): void {
    const p = this.pick()
    const before = this.hover?.id ?? ''
    this.hover = p
    // The thing a pinch would take lights before the pinch.
    const takes = !this.drag?.active && !this.two ? p : null
    const onCard = takes?.tok === 'card' || this.drag?.kind === 'card'
    if (onCard !== this.cardLit) { this.cardLit = onCard; this.askCard.dirty = true }
    // The Bot lights while the card is in its hand: that is how a hand knows that letting go there is a yes.
    const holder = this.cardIn ? this.asked() : undefined
    // What would take a thing that is carried lights too: the bin, or the Bot it is within reach of.
    const lit = litOf(takes, holder?.bot ?? '', this.dropLit)
    this.litId = lit?.id ?? (takes?.surface === this.choiceCard && takes.hit ? takes.id : '')
    if (lit?.kind === 'token') this.light(this.tokens.where(lit.id), lit.id === 'bin' ? 0.08 : MARBLE_R * 1.5)
    else if (lit?.kind === 'bot') this.light(this.world.botCentre(lit.id), botHeight(this.world.scale) * 0.75)
    else if (lit?.kind === 'thing') this.light(this.world.objectCentre(lit.id), 0.6 * (this.world.objectValue(lit.id)?.o.scale ?? 1) * (lit.id.startsWith('air:') || lit.id === 'screen' ? 1 : this.world.scale))
    else this.light(null, 0)
    // A card that is in the hand stays the hand's aim while it is carried.
    if (this.drag?.kind !== 'card') this.router.hover(this.active, this.routeId(p), clock)
    if ((p?.id ?? '') !== before) {
      for (const s of this.surfaces()) s.setHot(p?.surface === s ? (p.hit?.id ?? '') : '')
      this.opts.host.style.cursor = p && (p.hit || p.bot || p.obj || p.tok) ? 'pointer' : p?.ground ? 'grab' : 'default'
    }
    // Attention: the Bot under the ray.
    const on = this.drag?.active && this.drag.kind !== 'thing' ? '' : (p?.bot ?? '')
    if (on) { this.lastOn = clock; if (on !== this.attend) { this.attendTo(on, clock); this.focus = '' } }
    if (!on && !this.focus && this.attend && clock - this.lastOn > LINGER_MS) this.attendTo('', clock)
    const tip = p?.obj ?? ''
    if (tip !== this.tipId) { this.tipId = tip; this.tip.dirty = true }
    // A guarded hold shows its progress on the button itself.
    const held = this.router.pressedId
    for (const s of this.surfaces()) {
      const mine = held.startsWith(s.key + '/') ? { id: held.slice(s.key.length + 1), p: this.router.holdProgress(clock) } : null
      if (mine || s.state.hold) { s.state.hold = mine; s.dirty = true }
    }
  }

  /** Run `fn` with the ray of the hand that is `source`, when another hand is the one that aims. */
  private as(source: string, fn: () => void): void {
    const own = this.inXr && source !== this.active ? this.pointers.find(c => c.id === source) : undefined
    if (!own) { fn(); return }
    const ray = this.ray.clone()
    const seat = this.seatRay.clone()
    const aiming = this.aiming
    this.aimWith(own.ray, own.id)
    fn()
    this.aiming = aiming
    this.ray.copy(ray)
    this.seatRay.copy(seat)
  }

  /** What a pull out of a Bot, a thing or the screen brings into the hand: the page behind it. */
  private pullOut(id: string): Page | null {
    return id.startsWith('bot:') ? this.openPage('chat', id.slice(4)) : this.pull(id.slice(4))
  }

  private updateDrag(clock: number): void {
    const d = this.drag
    const pr = this.press
    if (d) this.as(d.source, () => this.dragStep(d, clock))
    if (d?.active) return
    // One hand drags the ground: the point that was taken hold of stays under the ray.
    const pan = this.pan
    if (pan && !this.two) {
      this.as(pan.source, () => {
        const g = this.world.groundAt(this.seatRay.origin, this.seatRay.direction)
        if (!g) return
        const dx = g.seat.x - pan.last.x
        const dz = g.seat.z - pan.last.z
        pan.moved += Math.hypot(dx, dz)
        if (pan.moved > 0.01) { this.router.cancel('scrolled'); this.world.pan(dx, dz) }
        pan.last.copy(g.seat)
      })
      return
    }
    // Press and pull on a page body scrolls it; that press can no longer click.
    if (pr?.surface && this.hover?.surface === pr.surface && this.hover.px && !d) {
      const page = this.pages.find(pg => pg.s === pr.surface)
      const y = this.hover.px[1]
      if (!pr.scrolling && Math.abs(y - pr.y0) > 60) { pr.scrolling = true; this.router.cancel('scrolled') }
      if (pr.scrolling && page) this.scrollBy(page, pr.yLast - y)
      pr.yLast = y
    }
  }

  /** One frame of a drag, with the ray of the hand that drags. */
  private dragStep(d: Drag, clock: number): void {
    // A pinch that has just opened may be over. Until that is known the thing stays where it was, and the hand's way is not read into a throw.
    if (this.pointers.find(c => c.id === d.source)?.openAt) return
    if (!d.active && clock - d.at > DRAG_MS && this.ray.direction.angleTo(d.dir) > DRAG_START) {
      d.active = true
      if (d.kind !== 'card') this.router.cancel('scrolled')
      // The grip brings the crew's page, and a pull on a Bot or a thing of the land brings its page: it comes out in the hand.
      const had = new Set(this.pages)
      const out = d.kind === 'grip' ? this.openPage('hub', '') : d.kind === 'pull' ? this.pullOut(d.pickId ?? '') : null
      if (d.kind === 'grip' || d.kind === 'pull') {
        if (!out) { this.drag = null; return }
        d.fresh = d.kind === 'pull' && !had.has(out)
        d.kind = 'move'
        d.page = out
      }
      // A row pulled out of its page pops free as a thing, and is carried.
      if (d.kind === 'thing') {
        const id = d.row ? this.pullRow(d.row) : ''
        if (!id) { this.drag = null; return }
        d.kind = 'carry'
        d.pickId = `obj:${id}`
        d.label = this.thingOf(d.pickId)?.label
      }
      // Where it was before the hand took it, and whether it is work that can be given to a Bot. A Bot's own page is not.
      if (d.kind === 'move' && d.page) d.home = { yaw: d.page.rec.yaw, pitch: d.page.rec.pitch }
      const pos = d.kind === 'carry' ? this.world.doc.objects.find(o => o.id === d.pickId?.slice(4))?.pos : undefined
      if (pos) d.homePos = [pos[0], pos[1], pos[2]]
      const what = d.kind === 'move' && d.page ? `page:${d.page.s.key}` : d.kind === 'carry' ? d.pickId ?? '' : ''
      const field = what ? this.thingOf(what)?.bind?.field ?? '' : ''
      d.gift = field.startsWith('thing:') || field.startsWith('goal:') ? what : ''
    }
    if (!d.active) return
    // How the hand turns is kept for a moment: a throw is read from it when the hand lets go.
    const dir = this.seatRay.direction
    d.samples.push({ t: clock, ...placeOf(dir.x, dir.y, dir.z) })
    while (d.samples.length > 2 && clock - d.samples[0].t > 400) d.samples.shift()
    if (d.kind === 'move' || d.kind === 'carry') {
      // What would take it if the hand let go now lights: the bin, or a Bot within reach. A Bot lights only once the thing
      // has been carried clear of every Bot, so work is not given straight back to the Bot it was just taken from.
      const near = this.botInReach()
      if (!near) d.clear = true
      this.dropLit = this.pick()?.tok === 'bin' ? 'bin' : d.clear && d.gift ? near : ''
      if (this.dropLit && this.dropLit !== 'bin') { this.litBot = this.dropLit; this.litAt = clock }
    }
    if (d.kind === 'move' && d.page) {
      // Work that is carried to be given is small and rides above the fingertip: the Bot under it stays in sight.
      if (d.gift) d.page.scale = TUNE.carryScale
      Object.assign(d.page.rec, this.pagePlace(d.page, !!d.gift))
    } else if (d.kind === 'resize' && d.page && d.startDist) {
      const centre = d.page.s.at.getWorldPosition(v3())
      const now = this.ray.at(centre.distanceTo(this.ray.origin), v3()).distanceTo(centre)
      // Never below full size: smaller would put the text under the reading floor.
      d.page.scale = Math.max(1, Math.min(1.6, (d.startScale ?? 1) * (now / d.startDist)))
    } else if (d.kind === 'carry' && d.pickId) this.nudge(d.pickId.slice(4), tuple(this.seatRay.at(AIR.distance, v3())))
    else if (d.kind === 'card') {
      // Within reach of its Bot the card goes into the Bot's hand. Anywhere else it is in the person's.
      const n = this.asked()
      const hand = n ? this.world.botCentre(n.bot) : null
      this.cardBotM = n ? this.cardToBot(n) : null
      this.cardIn = !!n && !!hand && (this.cardBotM ?? Infinity) <= this.handReach()
      // The rule reads the Bot's middle. The card is put where the body holds it: in front, so it is seen.
      this.cardTo.copy((this.cardIn ? this.world.botHand(n?.bot ?? '', this.rig.worldToLocal(this.head.clone())) : null) ?? this.cardHeld(d.startDist ?? AIR.distance))
    }
  }

  /** Where the card is while a hand carries it: just above the fingertip, so the Bot it goes back to stays in sight under it. */
  private cardHeld(dist: number): Vector3 {
    return this.seatRay.at(dist, v3()).add(v3(0, (this.askCard.height * OFFER_SCALE) / 2 + 0.02, 0))
  }

  /** Where a page that a hand holds is: hung from its header at the fingertip, or, carried small, riding just above it. */
  private pagePlace(pg: Page, above: boolean): { yaw: number; pitch: number } {
    const h = pg.s.height * pg.scale
    const at = this.seatRay.at(FULL.distance, v3()).add(v3(0, above ? h / 2 + 0.03 : -(h / 2 - 0.02), 0))
    return placeOf(at.x, at.y, at.z)
  }

  private scrollBy(pg: Page, dy: number): void {
    const s = pg.s
    // What is under the pointer moves, so a press that is being held on it no longer counts.
    this.router.cancel('scrolled')
    // Chat counts from the newest line up; the other pages from the top down.
    s.scroll = Math.max(0, Math.min(s.maxScroll, s.scroll + (pg.rec.kind === 'chat' ? -dy : dy)))
    s.dirty = true
  }

  /** The plot the person looks at: under the head's own line of sight in a session, under the pointer in the preview. */
  private gazePlot(): string {
    const origin = this.rig.worldToLocal(this.head.clone())
    const dir = this.eye.getWorldDirection(v3()).applyQuaternion(this.rig.quaternion.clone().invert())
    const g = this.inXr ? this.world.groundAt(origin, dir) : this.world.groundAt(this.seatRay.origin, this.seatRay.direction)
    return g ? this.world.plotOf(g.x, g.z) : this.attend
  }

  /* ---------------- point and talk ---------------- */

  /** The hand that can point right now, as a sample for the detector. Null when there is none. */
  private pointer(clock: number): { source: string; sample: HandSample | null; tracked: boolean } {
    if (!this.inXr) {
      // The mouse stands in for a finger: there are no fingers to read, so it only has to rest.
      const sample: HandSample = { t: clock, origin: tuple(this.seatRay.origin), dir: tuple(this.seatRay.direction), shape: null, pinching: this.held.has('mouse') }
      return { source: 'mouse', sample, tracked: true }
    }
    const c = this.aimer()
    if (!c || !c.id.startsWith('hand')) return { source: c?.id ?? '', sample: null, tracked: false }
    const joints = (c.joints as Readonly<Record<string, Object3D | undefined>>) ?? {}
    const pos: Record<string, V3 | null> = {}
    for (const [name, j] of Object.entries(joints)) pos[name] = j?.visible ? tuple(j.getWorldPosition(v3())) : null
    const shape = fingerShape(pos as Joints)
    // A hand whose fingers cannot be read cannot be told from a hand that just moves, so it does not point.
    if (!shape) return { source: c.id, sample: null, tracked: true }
    return { source: c.id, sample: { t: clock, origin: tuple(this.seatRay.origin), dir: tuple(this.seatRay.direction), shape, pinching: this.held.has(c.id) }, tracked: true }
  }

  private updateTalk(clock: number): void {
    const ptr = this.pointer(clock)
    let det = this.detectors.get(ptr.source)
    if (!det) { det = new PointDetector(); this.detectors.set(ptr.source, det) }
    const pointing = det.feed(ptr.sample)
    this.pointingNow = pointing
    // With hands only no Bot listens: speech is parked behind `voice`. With it, the mouse talks on sample data only.
    if (!this.opts.voice || (!this.inXr && !this.opts.fixture)) return
    // A finger on the card a Bot holds out, or a hand that holds it, is on that request: words said now are about it.
    const asked = this.asked()
    const at = asked ? this.fingerOn(pointing) : null
    if (at && this.cardPoint && clock > this.cardPointAt) {
      const dt = (clock - this.cardPointAt) / 1000
      // Smoothed over about a tenth of a second, so one noisy frame is not a fast finger.
      this.cardSpeed += (at.distanceTo(this.cardPoint) / dt - this.cardSpeed) * (1 - Math.exp(-dt / 0.1))
    } else if (!at) this.cardSpeed = 0
    this.cardPoint = at
    this.cardPointAt = clock
    const onCard = !!at
    if (onCard !== this.cardSince > 0) this.cardSince = onCard ? clock : 0
    // What the finger does just after the first word decides whether that word was said on the card.
    const first = this.word
    if (first && first.on && this.cardSince === first.on) {
      first.until = clock
      if (clock - first.at <= TUNE.cardStayMs) first.fastest = Math.max(first.fastest, this.cardSpeed)
    }
    // The scripted voice of the sample data: the words arrive one by one, as speech would, from the moment the finger lands.
    // It says nothing into a sentence that began on the card, or that a test speaks: what is said there is the test's to say.
    if (this.opts.fixture && this.listenAt && !this.quiet && this.talk.phase !== 'idle') {
      const words = this.opts.voiceLine.split(' ')
      const n = Math.min(words.length, Math.floor((clock - this.listenAt - 500) / 190))
      if (n > 0) this.hear(words.slice(0, n).join(' '), clock)
    }
    // Once a Bot listens, a thing the finger rests on is what "this" means.
    if (this.talk.phase === 'listening' || this.talk.phase === 'hearing') {
      const title = pointing && this.hover && !this.hover.bot ? this.thingOf(this.grabId(this.hover))?.label ?? '' : ''
      if (title && title !== this.pointed) { this.pointed = title; this.caption.dirty = true }
    }
    // While a sentence waits its beat, no Bot starts to listen.
    // On the card, the Bot that holds it out is the one that listens.
    const ev = this.plan ? null : this.talk.point({ now: clock, pointing: pointing || onCard, bot: onCard && asked ? asked.bot : this.hover?.bot ?? '' })
    if (ev) this.onTalk(ev, ptr.source, clock)
    if (this.talk.phase === 'hearing') this.caption.dirty = true
    if (this.plan && clock - this.plan.at >= POINT.BEAT_MS) this.startPlan(clock)
  }

  /**
   * Where the finger that is on the card is, in the seat's space, or null when none is. A hand that holds the card is on it.
   * So is a fingertip that touches it. The mouse has no fingertip: it is on the card while it rests on it.
   */
  private fingerOn(pointing: boolean): Vector3 | null {
    if (this.drag?.kind === 'card') return this.cardTo.clone()
    if (!this.inXr) return pointing && this.hover?.tok === 'card' ? this.seatRay.at(this.cardAt.distanceTo(this.seatRay.origin), v3()) : null
    for (const c of this.pointers) {
      const tip = (c.joints as Readonly<Record<string, Object3D | undefined>>)?.['index-finger-tip']
      if (!tip?.visible) continue
      const w = tip.getWorldPosition(v3())
      const l = this.askCard.at.worldToLocal(w.clone())
      if (tipOnCard([l.x, l.y, l.z], this.askCard.width, this.askCard.height, this.askCard.at.scale.x)) return this.rig.worldToLocal(w)
    }
    return null
  }

  /** What point-and-talk just did. A sentence is sent the same way as any other send: through the press route. */
  private onTalk(ev: TalkEvent, source: string, clock: number): void {
    const id = `say:${ev.bot}`
    if (ev.type === 'notice') {
      // The Bot turns the moment the finger lands, and sound is kept from now: nobody waits before they talk.
      this.voiceSource = `${source}~voice`
      this.voice.hover(this.voiceSource, id, clock)
      this.listenAt = clock
      this.word = null
      this.quiet = this.cardSince > 0
      this.spoken = null
      this.pointed = ''
      this.note = null
      this.opts.speech?.start()
      this.sound.cue('arrive', this.at(ev.bot))
      this.say(this.S.listening)
      this.caption.dirty = true
      return
    }
    if (ev.type === 'listen') {
      // The Bot is the listener for good now. The hand is free to go back to work.
      this.voice.down(this.voiceSource, { id, guarded: true, version: this.sayVersion(ev.bot) }, clock)
      this.caption.dirty = true
      return
    }
    this.listenAt = 0
    if (ev.type === 'send') {
      // A sentence to the Bot that holds a card out may be the yes to it. What counts as one is read by `approval.ts`.
      const asked = this.asked()
      const w = this.word
      const onCard = !!w && onCardForWord({ wordAt: w.at, restAt: this.cardRest, onSince: w.on, onUntil: w.until, fastest: w.fastest })
      if (w && asked?.bot === ev.bot && answerOf({ onCard, words: ev.text, letGo: '' }, this.S) === 'yes') {
        // The yes is the answer and not a message: nothing goes to the chat. Its press began once the finger had rested on the card.
        this.voice.drop(this.voiceSource)
        this.confirm(asked, this.voiceSource, w.on + AIM_REST_MS, 0, true, ev.text, clock)
        this.caption.dirty = true
        return
      }
      // The Bot shows what it understood and starts after one beat. The recogniser stays on, to hear "never mind".
      this.plan = { bot: ev.bot, text: ev.text, about: this.holding(), at: clock }
      this.beads.set(ev.bot, { total: 1, done: 0, at: clock, lead: '', scripted: false })
      this.caption.dirty = true
      return
    }
    // Nothing was said, or it was taken back: nothing is sent. Words that were cut off are kept.
    this.opts.speech?.stop()
    this.voice.drop(this.voiceSource)
    if (ev.type === 'keep') this.keep(ev.bot, ev.text, this.S.sayKept)
    else if (ev.why === 'never-mind') this.keep(ev.bot, '', this.S.dropped)
    this.caption.dirty = true
  }

  /** The beat is over: the sentence goes to the press route, and the Bot starts. */
  private startPlan(clock: number): void {
    const pl = this.plan
    if (!pl) return
    this.plan = null
    this.beat = Math.round(clock - pl.at)
    this.opts.speech?.stop()
    // A sentence to the Bot that holds a card out may be the no to it. (A yes was taken when the sentence ended.)
    const asked = this.asked()
    const answer = asked?.bot === pl.bot ? answerOf({ onCard: false, words: pl.text, letGo: '' }, this.S) : 'nothing'
    // "This" in the sentence is named in what is sent.
    this.spoken = { bot: pl.bot, text: pl.about ? `${pl.text}\n${this.S.aboutThis(pl.about)}` : pl.text }
    this.sayDone = false
    this.voice.up(this.voiceSource, `say:${pl.bot}`, clock, true)
    // The route either wrote or refused, and said so. If it did neither, the words are still kept.
    if (!this.sayDone) this.keep(pl.bot, this.spoken.text, this.S.sayKept)
    // A no refuses that request. The words have gone to the Bot as the reason, and its caption repeats them.
    if (answer === 'no' && asked) { this.reason = { need: asked.id, text: pl.text }; this.answer(asked.id, 'no', this.voiceSource, pl.at, clock, this.voice) }
    // On sample data the Bot is scripted: it holds up the steps of its scripted reply. A real Bot's beads come with its reply.
    const steps = this.opts.fixture ? planOf(this.opts.voiceReply) : []
    if (steps.length) this.beads.set(pl.bot, { total: steps.length, done: 0, at: clock, lead: leadOf(this.opts.voiceReply), scripted: true })
    else this.beads.delete(pl.bot)
  }

  /** "Never mind", in the beat: the sentence that was just said is dropped, and nothing starts. */
  private cancelPlan(): boolean {
    const pl = this.plan
    if (!pl) return false
    this.plan = null
    this.beads.delete(pl.bot)
    this.opts.speech?.stop()
    this.voice.drop(this.voiceSource)
    this.keep(pl.bot, '', this.S.dropped)
    return true
  }

  /** A palm, or its key: "Stop. Not now." A sentence under way is dropped and the Bot's card goes back to the tray as a marble. Nothing is refused. */
  private stop(slug: string, clock: number): void {
    if (!this.bot(slug)) return
    if (this.crew.needs.find(n => n.id === this.pending?.need)?.bot === slug) this.cancelPending()
    if (this.plan?.bot === slug) this.cancelPlan()
    else if (this.talk.bot === slug) { const ev = this.talk.drop(); if (ev) this.onTalk(ev, '', clock) }
    if (this.choices?.bot === slug) this.choices = null
    if (this.asked()?.bot === slug) {
      this.tokens.offer = ''
      if (this.drag?.kind === 'card') { this.router.drop(this.drag.source); this.drag = null }
    }
    this.world.nodes.get(slug)?.body?.setPose('stand')
    this.stops += 1
    this.sound.cue('refuse', this.at(slug))
    this.keep(slug, '', this.S.stopped)
  }

  /** A pat: thanks, and nothing else. */
  private thank(slug: string): void {
    const b = this.bot(slug)
    if (!b) return
    this.world.nodes.get(slug)?.body?.hop()
    this.pats += 1
    this.sound.cue('reply', this.at(slug))
    this.say(this.S.thanked(b.label))
  }

  /** The Bot a stop is meant for: the one that holds a card out, has a sentence, or listens. '' for none. */
  private dealing(): string {
    return this.asked()?.bot || this.choices?.bot || this.plan?.bot || this.talk.bot
  }

  /** Whether a hand is at the card a Bot holds out: its ray is on the card, or its fingertip touches it. */
  private onCardRay(c: Pointer): boolean {
    if (!this.askCard.at.visible) return false
    const tip = (c.joints as Readonly<Record<string, Object3D | undefined>>)?.['index-finger-tip']
    const l = tip?.visible ? this.askCard.at.worldToLocal(tip.getWorldPosition(v3())) : null
    if (l && tipOnCard([l.x, l.y, l.z], this.askCard.width, this.askCard.height, this.askCard.at.scale.x)) return true
    const o = this.over.get(c.id)
    return !!o && o.frame >= this.frameNo - 1 && o.pick?.tok === 'card'
  }

  /** Read the open hands of a session: a palm held up at a Bot stops it, and two taps on top of one thank it. */
  private updateHands(clock: number): void {
    const reach = Math.max(0.05, botHeight(this.world.scale))
    // First what each hand is: open or not, and where its wrist is.
    const hands = this.pointers.map(c => {
      const joints = (c.joints as Readonly<Record<string, Object3D | undefined>>) ?? {}
      const at = (name: string): V3 | null => { const j = joints[name]; return j?.visible ? tuple(j.getWorldPosition(v3())) : null }
      const [wrist, index, pinky, tip] = [at('wrist'), at('index-finger-metacarpal'), at('pinky-finger-metacarpal'), at('middle-finger-tip')]
      const pos: Record<string, V3 | null> = {}
      for (const name of Object.keys(joints)) pos[name] = at(name)
      // A hand that reaches for the card is about to take it: it is not telling the card's Bot to stop.
      const open: OpenHand | null = wrist && index && pinky && tip && (c.handed === 'left' || c.handed === 'right') && !this.held.has(c.id) && !this.onCardRay(c)
        ? { joints: { wrist, index, pinky }, handed: c.handed, shape: fingerShape(pos as Joints), tip } : null
      return { c, open, wrist }
    })
    // Both palms forward: everyone stop. Both hands pulled back after that: carry on.
    const left = hands.find(h => h.c.handed === 'left')
    const right = hands.find(h => h.c.handed === 'right')
    const verb = this.both.feed(left?.open ?? null, right?.open ?? null, [left?.wrist ?? null, right?.wrist ?? null], tuple(this.head), clock)
    if (verb === 'stop') this.stopAll(clock)
    else if (verb === 'carry-on') this.carryOn()
    for (const { c, open } of hands) {
      // A palm is for the Bot that is being dealt with; with none, for the Bot it faces most squarely.
      const dealt = this.dealing()
      let stopFor = ''
      let best: number = PALM.FACING
      let over: number | null = null
      let patted = ''
      for (const b of open ? this.crew.bots : []) {
        const top = this.at(b.slug)
        const facing = open && (!dealt || dealt === b.slug) ? palmToward(open, top) : 0
        if (facing >= best) { best = facing; stopFor = b.slug }
        const above = open ? palmOver(open, top, reach) : null
        if (above !== null && (over === null || above < over)) { over = above; patted = b.slug }
      }
      let dwell = this.palms.get(c.id)
      if (!dwell) { dwell = new Dwell(PALM.HOLD_MS); this.palms.set(c.id, dwell) }
      // One palm of two that are both up is not "not now" to one Bot.
      if (dwell.feed(!!stopFor && !this.both.both, clock)) this.stop(stopFor, clock)
      let pat = this.patting.get(c.id)
      if (!pat) { pat = new PatDetector(); this.patting.set(c.id, pat) }
      if (pat.feed(over, reach, clock)) this.thank(patted)
    }
  }

  /**
   * A finger that rests on a thing asks why: the thing opens into its parts, with the one in trouble lit.
   * A tap asks nothing, and neither does a hand that passes over.
   */
  private updateWhy(clock: number): void {
    const under = this.hover?.obj ? `obj:${this.hover.obj}` : ''
    if (under !== this.whyDone) this.whyDone = ''
    const on = this.pointingNow && !this.drag && !this.two && !this.held.size && under !== this.whyDone ? under : ''
    if (on !== this.whyOn) { this.whyOn = on; this.whyDwell.feed(false, clock) }
    if (!this.whyDwell.feed(!!on, clock) || this.opening.isOpen(on)) return
    const thing = this.thingOf(on)
    if (!thing?.bind) return
    this.opening.show(thing)
    this.whyOpen = on
    this.sound.cue('arrive', tuple(this.head))
    this.dirtyAll()
  }

  /** Words arrive. Where the finger is as the first one starts is kept: a yes needs it on the card then. */
  private hear(text: string, clock: number): void {
    // shortcut: the first result stands for the start of the word. A recogniser that gives word times should pass them: a late result moves the word later.
    if (text.trim() && this.talk.phase !== 'idle' && !this.word) this.word = { at: clock, on: this.cardSince, until: this.cardSince ? clock : 0, fastest: this.cardSpeed }
    this.talk.heard(text, clock)
  }

  /** The recogniser's words so far, and its word that the sentence is complete. */
  heard(text: string): void {
    if (!this.opts.voice) return
    const clock = performance.now()
    // "No" or "wait" in the beat after a yes stops it.
    if (this.pending && stopsYes(text, this.S)) { this.cancelPending(); this.dirtyAll(); return }
    // "Never mind" drops the sentence: the one that waits its beat, or the one that is still being said.
    if (endsWith(text, this.S.neverMindWords)) {
      const ev = this.cancelPlan() ? null : this.talk.drop()
      if (ev) this.onTalk(ev, '', clock)
      return
    }
    this.quiet = true
    this.hear(text, clock)
  }
  heardEnd(): void { const ev = this.opts.voice ? this.talk.endpoint() : null; if (ev) this.onTalk(ev, '', performance.now()) }

  /* ---------------- per frame ---------------- */

  /** One frame. The framework's loop calls it, in the preview and in a session, and draws the picture afterwards. */
  step(): void {
    if (this.disposed) return
    const clock = performance.now()
    const dt = Math.min(0.05, (clock - this.lastT) / 1000)
    this.lastT = clock
    const still = this.opts.reducedMotion
    const k = still ? 1 : 1 - Math.exp(-dt * 9)
    if (this.inXr) this.updateXr()
    else this.updatePreview(k)
    this.eye.getWorldPosition(this.head)
    const fwd = this.eye.getWorldDirection(v3())
    this.sound.listen(tuple(this.head), tuple(fwd))
    this.seatRay.origin.copy(this.rig.worldToLocal(this.ray.origin.clone()))
    this.seatRay.direction.copy(this.ray.direction).applyQuaternion(this.rig.quaternion.clone().invert())
    this.updateHover(clock)
    this.updateDrag(clock)
    this.updateTalk(clock)
    this.updatePending(clock)
    this.updateWhy(clock)
    if (this.inXr) this.updateHands(clock)
    if (this.wheelAt && clock - this.wheelAt > WHEEL_REST_MS) { this.wheelAt = 0; this.zoomPlot = null; this.world.release() }
    const listening = this.talk.phase === 'idle' ? this.attend : this.talk.bot
    this.world.update(dt, clock, this.opts.now() / 1000, still, this.rig.position.y, this.rig.worldToLocal(this.head.clone()), listening)
    this.updateSurfaces(clock, k, dt)
    this.syncTargets()
    this.syncRouter(clock)
    // The places of the scene are not drawn, so nothing else brings them up to date: the parts that draw read them next.
    this.root.updateMatrixWorld(true)
    this.frameNo += 1
    if ((this.opts.fixture || this.opts.probe) && clock - this.probeAt > 150) { this.probeAt = clock; this.writeProbe() }
  }

  /**
   * Which surfaces a pointer of the framework can be on, and which comes first where two overlap. A card that is only
   * for reading is in nobody's way, a surface that is not shown is not there, and one a hand carries is not in its own way.
   */
  private syncTargets(): void {
    const reading = [this.brief, this.caption, this.bubble, this.tip, this.chip, this.status, ...this.opening.cards]
    const carried = this.drag?.kind === 'card' ? this.askCard : this.drag?.active && this.drag.kind === 'move' ? this.drag.page?.s : undefined
    for (const s of this.surfaces()) {
      s.at.pointerEvents = s.at.visible && s !== carried && !reading.includes(s) ? 'auto' : 'none'
      // The card a Bot holds out is nearest the hand: it comes before everything. Then what is drawn on top.
      s.at.pointerEventsOrder = s === this.askCard ? ORDER.card : ORDER.surface + s.at.renderOrder / 100
    }
  }

  /**
   * The Bots, the things and the tokens a pointer can be on: where each is in the seat's space, and how big. None is
   * smaller than the least angle a hand can aim at, so a Bot drawn tiny in the miniature can still be pointed at.
   */
  targets(): { id: string; at: Vector3; radius: number; order: number }[] {
    const eye = this.rig.worldToLocal(this.head.clone())
    const out: { id: string; at: Vector3; radius: number; order: number }[] = []
    const add = (id: string, at: Vector3 | null, size: number, deg: number, order: number, more = 0) => {
      if (at) out.push({ id, at, radius: Math.max(size, at.distanceTo(eye) * Math.tan(deg * DEG)) + at.distanceTo(eye) * Math.tan(more * DEG), order })
    }
    // The Bot that listens keeps its turn a little past its edge.
    for (const b of this.crew.bots) add(`bot:${b.slug}`, this.world.botCentre(b.slug), botHeight(this.world.scale) / 2, POINT.PICK_DEG, ORDER.land, b.slug === this.talk.bot ? POINT.EXIT_DEG : 0)
    // A thing a hand carries is not in its own way. A thing in the air is nearer than any page.
    const carried = this.drag?.active && this.drag.kind === 'carry' ? this.drag.pickId : ''
    for (const o of this.world.doc.objects) if (`obj:${o.id}` !== carried) add(`obj:${o.id}`, this.world.objectCentre(o.id), this.world.objectSize(o.id), OBJECT_PICK_DEG, o.id.startsWith('air:') ? ORDER.air : ORDER.land)
    for (const id of this.tokens.ids()) add(`tok:${id}`, this.tokens.where(id), id === 'bin' ? 0.08 : MARBLE_R, TOKEN_PICK_DEG, ORDER.token)
    return out
  }

  /** How high the ground is, in the seat's space: where a pointer on nothing else meets the land. */
  get groundY(): number { return this.world.land.position.y }
  /** The seat: the person's own space. Parts that are drawn in it are put here. */
  get seat(): Group { return this.rig }

  private syncRouter(clock: number): void {
    const targets: RouteTarget[] = []
    for (const s of this.surfaces()) {
      if (!s.at.visible) continue
      for (const h of s.hits) if (h.guarded) targets.push({ id: `${s.key}/${h.id}`, guarded: true, version: h.version })
    }
    // Every Bot can be spoken to, so a spoken sentence is a guarded target like a Send button. It has its own press.
    // The request whose card is held out is the one that can be answered, and only while it is held out:
    // by hand on one press, by word on the other, and guarded the same way on both.
    const asked = this.asked()
    const answers = asked ? ['yes', 'no'].map(a => ({ id: `${a}:${asked.id}`, guarded: true, version: asked.version })) : []
    this.voice.sync([...this.crew.bots.map(b => ({ id: `say:${b.slug}`, guarded: true, version: this.sayVersion(b.slug) })), ...answers], clock)
    targets.push(...answers)
    // The tray follows what waits: a new marble rolls in from its Bot's plot.
    const waiting = this.crew.needs.map(n => n.id)
    if (waiting.join('|') !== this.trayKey) { this.trayKey = waiting.join('|'); this.tokens.setNeeds(waiting, id => this.world.botTop(this.crew.needs.find(n => n.id === id)?.bot ?? ''), clock) }
    this.router.sync(targets, clock)
  }

  private updatePreview(k: number): void {
    // A small head sway with the pointer, so depth can be seen on a flat screen. A framed view holds still: it is measured.
    const sway = (v: number) => Math.max(-1, Math.min(1, v))
    const goal = this.view ? v3(0, EYE_Y, 0) : v3(sway(this.mouse.x) * 0.05, EYE_Y + sway(this.mouse.y) * 0.025, 0)
    this.camera.position.lerp(goal, k)
    const look = this.view ? this.opts.look : PREVIEW_LOOK
    this.camera.lookAt(this.camera.position.x * 0.4, EYE_Y - Math.tan(look * DEG), -1)
    this.camera.updateMatrixWorld()
  }

  private sendTarget(): RouteTarget | null {
    const page = this.pages.find(pg => pg.rec.slug === this.typing && pg.rec.kind === 'chat' && !pg.rec.stored)
    return page ? { id: `${page.s.key}/send`, guarded: true, version: `${this.typing}|${this.drafts.get(this.typing) ?? ''}` } : null
  }

  key(e: KeyboardEvent): boolean {
    if (e.ctrlKey || e.metaKey || e.altKey) return false
    const clock = performance.now()
    if (this.typing) {
      const text = this.drafts.get(this.typing) ?? ''
      if (e.key === 'Enter') {
        // Enter is the same guarded press as a pinch: hold it, then let go. A tap sends nothing.
        const t = this.sendTarget()
        if (!e.repeat && t && !this.keyHeld) { this.keyHeld = t.id; this.router.hover('key', t.id, clock - AIM_REST_MS); this.router.down('key', t, clock) }
        return true
      }
      if (e.key === 'Escape') this.typing = ''
      else if (e.key === 'Backspace') this.drafts.set(this.typing, [...text].slice(0, -1).join(''))
      else if (e.key.length === 1) this.drafts.set(this.typing, (text + e.key).slice(0, 2000))
      else return false
      this.router.cancel('card-changed')
      this.dirtyAll()
      return true
    }
    const bots = this.crew.bots
    const at = bots.findIndex(b => b.slug === (this.focus || this.attend))
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key
    const on = this.focus || this.attend
    switch (k) {
      case 'ArrowRight': case 'ArrowLeft': {
        if (!bots.length) return false
        const next = bots[(at + (k === 'ArrowRight' ? 1 : bots.length - 1) + (at < 0 && k === 'ArrowLeft' ? 1 : 0)) % bots.length]
        this.focus = next.slug
        this.attendTo(next.slug, clock)
        this.world.goTo(next.slug)
        this.say(next.label)
        break
      }
      case ' ': case 'Enter':
        if (on) this.openPage('chat', on)
        else return false
        break
      case 'Escape': {
        // One press undoes the newest thing: a yes that waits its beat, a sentence, a card held out, an open thing, the bin, a page.
        if (this.cancelPending()) break
        if (this.choices) { this.choices = null; break }
        const ev = this.plan ? null : this.talk.stop()
        if (this.cancelPlan()) break
        else if (ev) this.onTalk(ev, '', clock)
        else if (this.tokens.offer) this.tokens.offer = ''
        else if (this.opening.thing) this.opening.shut()
        else if (this.shelfOpen) this.shelfOpen = false
        else if (this.pages.some(pg => !pg.rec.stored)) this.closePage([...this.pages].reverse().find(pg => !pg.rec.stored))
        else if (this.focus || this.attend) { this.focus = ''; this.attendTo('', clock) } else return false
        break
      }
      // The keys that stand for the two hands: in toward the plot under the pointer, out, and all the way out.
      case '=': case '+': this.world.zoom(1.7, this.gazePlot() || on || bots[0]?.slug || ''); this.world.release(); break
      case '-': this.world.zoom(1 / 1.7, ''); this.world.release(); break
      case 'g': this.world.whole(); break
      // The key that stands for two hands on one thing: it opens the thing under the pointer into its parts, or shuts it.
      case 'o': { const thing = this.thingOf(this.grabId(this.hover) || (on ? `bot:${on}` : '')); if (thing) this.opening.toggle(thing); else return false; break }
      // The keys that stand for an open hand: a palm at the Bot that is being dealt with, and a pat on the Bot under the pointer.
      case 'p': { const to = this.dealing() || on; if (to) this.stop(to, clock); else return false; break }
      case 'a': if (on) this.thank(on); else return false; break
      // The keys that stand for two open hands: both palms forward (everyone stop), and both hands pulled back (carry on).
      case 'x': this.stopAll(clock); break
      case 'u': if (this.halted) this.carryOn(); else return false; break
      // Sample data only: the scripted voice says "never mind".
      case 'm': if (this.opts.fixture && (this.plan || this.talk.phase !== 'idle')) this.heard(`${this.plan?.text ?? this.talk.text} ${this.S.neverMindWords.split('|')[0]}`); else return false; break
      case 'h': this.setHand(!this.hand); break
      case 'n': this.tool('next'); break
      case 's': this.tool('shelf'); break
      case 'f': this.openPage('hub', ''); break
      case 'c': this.placed = false; break
      case 'd': if (on) this.openPage('dash', on); else return false; break
      case 'w': if (on) this.openPage('work', on); else return false; break
      default: return false
    }
    this.dirtyAll()
    return true
  }

  keyUp(e: KeyboardEvent): void {
    if (e.key !== 'Enter' || !this.keyHeld) return
    const id = this.keyHeld
    this.keyHeld = ''
    this.router.up('key', id, performance.now(), true)
  }

  /** True while keys go into a draft, so the page keeps its own shortcuts out of the way. */
  get isTyping(): boolean { return !!this.typing }

  /** Pull out a Bot's chat at once. For the page's own list of Bots, which a screen reader and Tab use. */
  open(slug: string): void { this.openPage('chat', slug); this.dirtyAll() }

  private listen(): void {
    const canvas = this.opts.host
    const on = (t: EventTarget, type: string, fn: (e: Event) => void, o?: AddEventListenerOptions) => {
      t.addEventListener(type, fn, o)
      this.cleanup.push(() => t.removeEventListener(type, fn, o))
    }
    // The wheel is ours: it must be able to stop the page from scrolling, which a listener of the framework's cannot.
    on(canvas, 'wheel', e => {
      e.preventDefault()
      const p = this.pick()
      const page = this.pages.find(pg => pg.s === p?.surface)
      if (page) { this.scrollBy(page, (e as WheelEvent).deltaY); return }
      // Off a page, the wheel is the two hands: pulled apart toward the plot under the pointer, or pushed together.
      this.zoomPlot ??= this.gazePlot()
      this.world.zoom(Math.exp(-(e as WheelEvent).deltaY * 0.0016), this.zoomPlot)
      this.wheelAt = performance.now()
    }, { passive: false })
    // A mouse that leaves the picture points at nothing. The framework's pointer goes quiet then; the box says that it left.
    on(canvas, 'pointerleave', () => this.mouseGone())
    on(window, 'blur', () => this.away('blur'))
    on(document, 'visibilitychange', () => { if (document.hidden) this.away('hidden') })
  }

  /* ---------------- the framework's pointers: where each one aims, and when it presses ---------------- */

  /**
   * A pointer of the framework moved: the mouse of the preview, or a hand or a device of a session. `ray` is where it
   * aims; `at` is where the mouse is on the picture, from -1 to 1 across and up.
   */
  aim(source: string, ray: Ray, at?: Vector2): void {
    if (source === 'mouse') {
      if (at) this.mouse.copy(at)
      if (this.inXr) return
      this.aimWith(ray, 'mouse')
      return
    }
    this.pointers.find(c => c.id === source)?.ray.copy(ray)
  }

  /** The mouse left the picture: it points at nothing. */
  mouseGone(): void {
    if (this.inXr || this.held.size) return
    this.mouse.set(0, 3)
    this.ray.direction.set(0, 1, 0)
  }

  /**
   * The mouse of the preview pressed, let go, or lost its press to the system. `at` is when it let go: the framework
   * tells of a release at the next frame, and a throw is read from where the mouse was and when.
   */
  mouseDown(): void { if (this.inXr) return; this.updateHover(performance.now()); this.down('mouse') }
  mouseUp(at: number): void { if (!this.inXr) this.up('mouse', true, false, at) }
  mouseCancel(): void { this.router.cancel('system'); this.up('mouse', false) }

  /** An immersive session began. The framework holds it; from here the hands aim and the head is the viewer pose. */
  sessionStarted(): void {
    this.inXr = true
    this.placed = false
    this.xrFrames = 0
    this.active = 'head'
    this.rig.add(this.strip.at)
    this.strip.at.rotation.set(0, 0, 0)
  }

  /** The session is no longer in front of the person: a press under way must not count. */
  sessionHidden(): void { this.away('hidden') }

  /**
   * A hand or a device joined the session. It is named by what the browser says it is: a hand, or a device that can
   * never approve. The page writes where it aims, and where a hand's joints are, into the pointer each frame.
   */
  addSource(source: XrSourceLike, joints: readonly string[]): Pointer {
    const c: Pointer = {
      source, ray: new Ray(), joints: Object.fromEntries(joints.map(name => { const j = new Object3D(); j.visible = false; return [name, j] })),
      handed: source.handedness ?? '', id: sourceIdFor(source, this.pointers.length), openAt: 0, letGo: () => undefined as void,
    }
    // A tracker drops a pinch for a frame or two. So a pinch counts as let go only once it has stayed open a moment.
    c.letGo = () => {
      c.openAt = 0
      if (this.two && (this.two.a === c || this.two.b === c)) { this.held.delete(c.id); this.endTwo(); return }
      this.aimWith(c.ray, c.id)
      this.up(c.id, this.completed.has(c.id), true)
    }
    this.pointers.push(c)
    return c
  }

  /** A hand that goes out of sight ends a pinch. A sentence that is being spoken has a press of its own, and goes on: its pause ends it. */
  removeSource(c: Pointer): void {
    // A session that ended has already let every hand go.
    if (!this.pointers.includes(c)) return
    c.handed = ''
    c.openAt = 0
    this.held.delete(c.id)
    this.endTwo()
    this.router.cancel('tracking-lost')
    this.pointers = this.pointers.filter(x => x !== c)
  }

  /** The pinch of a hand, or the trigger of a device, as the session reports it: it began, it completed, it ended. */
  select(c: Pointer, what: 'start' | 'done' | 'end'): void {
    // A press commits only when the system says the pinch completed: `select` comes before `selectend`.
    if (what === 'done') { this.completed.add(c.id); return }
    if (what === 'end') {
      c.openAt = performance.now()
      // Where the hand points as its pinch opens is the last of what a throw is read from.
      const d = this.drag
      if (!d?.active || d.source !== c.id) return
      this.aimWith(c.ray, c.id)
      d.samples.push({ t: c.openAt, ...placeOf(this.seatRay.direction.x, this.seatRay.direction.y, this.seatRay.direction.z) })
      return
    }
    // The same pinch, back after a dropped frame: it goes on as if it had never opened.
    if (c.openAt) { c.openAt = 0; return }
    this.aimWith(c.ray, c.id)
    const other = this.pointers.find(x => x !== c && this.held.has(x.id))
    // Two hands on the same thing go in: a thing opens into its parts, and a page, which has no parts, only gets bigger.
    // Two hands with the land, a Bot or nothing between them take the land.
    const grab = other && this.press?.source === other.id ? this.press.grab : ''
    const same = !!grab && !!other && grab === this.grabId(this.opts.twoHand === 'double' ? this.midPick(other, c) : this.pick())
    const sized = same ? this.pages.find(pg => `page:${pg.s.key}` === grab) : undefined
    const thing = same && !sized ? this.thingOf(grab) : null
    if (other && !this.drag?.active && (thing || sized || !this.press?.surface)) {
      const dist = other.ray.origin.distanceTo(c.ray.origin)
      this.held.add(c.id)
      this.router.cancel('second-source')
      this.pan = null
      this.drag = null
      this.press = null
      this.two = { a: other, b: c, dist, open: !!thing, page: sized, scale0: sized?.scale ?? 1, track: [] }
      if (thing) this.opening.take(thing, dist)
      else if (!sized) this.zoomPlot = this.gazePlot()
      return
    }
    this.down(c.id)
  }

  /** The two hands let go of what was between them. The press that began the hold is spent, and commits nothing. */
  private endTwo(): void {
    const two = this.two
    if (!two) return
    this.two = null
    const plot = this.zoomPlot ?? ''
    this.zoomPlot = null
    if (two.open) this.opening.release()
    else if (!two.page) {
      // Let go while the land still moves fast, it carries on to the end by itself. Let go at rest, it stays.
      const fling = zoomFling(two.track)
      if (fling) this.world.zoom(fling > 0 ? 1e9 : 1e-9, fling > 0 ? plot : '')
      this.world.release()
    }
    this.router.cancel('second-source')
  }

  /** The hand that aims: the right one, unless it is carrying something and the other is free. Then the free hand points, and can talk. */
  private aimer(): Pointer | undefined {
    const right = this.pointers.find(c => c.handed === 'right') ?? this.pointers.find(c => c.handed)
    const free = this.pointers.find(c => c.handed && c !== right)
    return right && free && this.drag?.active && this.drag.source === right.id ? free : right
  }

  /** The session ended, by the person or by the system. The page is the desktop preview again. */
  sessionEnded(): void {
    this.away('session-end')
    const ev = this.talk.stop()
    if (ev) this.onTalk(ev, '', performance.now())
    this.inXr = false
    this.active = 'mouse'
    this.endTwo()
    this.drag = null
    this.held.clear()
    this.pointers = []
    this.rig.position.set(0, EYE_Y, 0)
    this.rig.rotation.set(0, 0, 0)
    this.parkStrip()
    this.setHand(false)
  }

  private aimWith(ray: Ray, source: string): void {
    this.aiming = source
    this.ray.copy(ray)
    this.seatRay.origin.copy(this.rig.worldToLocal(this.ray.origin.clone()))
    this.seatRay.direction.copy(this.ray.direction).applyQuaternion(this.rig.quaternion.clone().invert())
  }

  private updateXr(): void {
    const head = this.viewer.getWorldPosition(v3())
    const fwd = this.viewer.getWorldDirection(v3())
    this.xrFrames += 1
    // The first frames of a session still carry the preview's camera. Wait for the real head pose.
    if (!this.placed && this.xrFrames > 2 && head.lengthSq() > 0) {
      // The seat is where the head is, facing where it looks. "Recenter" does this again.
      this.rig.position.copy(head)
      this.rig.rotation.set(0, Math.atan2(-fwd.x, -fwd.z), 0)
      this.rig.updateMatrixWorld()
      this.placed = true
    }
    const clock = performance.now()
    for (const c of this.pointers) if (c.openAt && clock - c.openAt >= TUNE.pinchOpenMs) c.letGo()
    const aim = this.aimer()
    // Until a hand points, the head aims.
    if (aim) { this.aimWith(aim.ray, aim.id); this.active = aim.id } else { this.ray.origin.copy(head); this.ray.direction.copy(fwd); this.active = 'head'; this.aiming = 'head' }
    // Two hands hold one thing. The land grows toward the plot looked at as they move apart, or shrinks; a thing opens, or shuts.
    // A pinch that has just opened may be over: until that is known nothing moves, and the hands' way is not read into a fling.
    if (this.two && !this.two.a.openAt && !this.two.b.openAt) {
      const d = this.two.a.ray.origin.distanceTo(this.two.b.ray.origin)
      if (this.two.page) this.two.page.scale = Math.max(1, Math.min(1.6, (this.two.scale0 * d) / this.two.dist))
      else if (this.two.open) this.opening.pull(d, this.opts.twoHand)
      else {
        this.world.zoom(handZoom(this.two.dist, d, this.world.sMin, this.opts.twoHand), this.zoomPlot ?? '')
        this.two.dist = d
        // How the zoom has gone over the last moment: what a fling is read from when the hands let go.
        const now = performance.now()
        this.two.track = [...this.two.track.filter(x => now - x.at <= 400), { at: now, t: this.world.goal.t }]
      }
    }
    const left = this.pointers.find(c => c.handed === 'left')
    const joints: Readonly<Record<string, Object3D | undefined>> | undefined = left?.joints
    const at = (name: string): V3 | null => {
      const j = joints?.[name]
      if (!j || !j.visible) return null
      const p = j.getWorldPosition(v3())
      return [p.x, p.y, p.z]
    }
    const wrist = at('wrist'), index = at('index-finger-metacarpal'), pinky = at('pinky-finger-metacarpal')
    const view = wrist && index && pinky ? palmView({ wrist, index, pinky }, 'left', [head.x, head.y, head.z], [fwd.x, fwd.y, fwd.z]) : null
    const was = this.hand
    this.setHand(stripShown(view, was))
    if (was && !this.hand) this.router.cancel('tracking-lost')
    if (this.hand && wrist && index) {
      const palm = v3((wrist[0] + index[0]) / 2, (wrist[1] + index[1]) / 2 + 0.09, (wrist[2] + index[2]) / 2)
      this.strip.at.position.copy(this.rig.worldToLocal(palm))
      this.strip.at.lookAt(head)
    }
  }

  /* ---------------- test probe (sample data, or asked for) ---------------- */

}
