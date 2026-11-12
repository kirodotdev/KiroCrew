/**
 * The live-browser card's writes (RFC: live browser in chat, "Wire contract").
 *
 * Every route is owner-only on the gateway: it accepts the dashboard owner's
 * cookie or token and refuses the internal secret with 403, so an agent cannot
 * claim input for itself or ack frames it never saw. The card sends bodies in
 * FRAME pixels; nothing here converts.
 */

import { TAB_ID } from '../tabId'
import type { ClientTransport } from './transport'
import type { BrowserInputEvent } from '../../hooks/useBrowserLive.input'
import type { QuickAskAction, QuickAskValues } from '../../pages/chat/quickAsk/types'

export type { BrowserInputEvent }

export interface BrowserLiveOk { ok: boolean }

/**
 * The window event a hand-back note becomes when no agent op is waiting to read
 * it: `{ slotKey, text }`. The chat page sends `text` as the person's message.
 */
export const BROWSER_HANDBACK_CHAT_EVENT = 'mc:browser-handback-chat'

/** What the person says as they hand back; either part may be left out. */
export interface BrowserHandbackSaid { note?: string; choice?: string }
export interface BrowserLiveRelease extends BrowserLiveOk { delivered?: '' | 'agent' | 'chat' }

/** The recordings routes' prefix for one session (the session key is a path segment). */
export function browserLiveRecordingsPath(sessionKey: string): string {
  return `/api/browser/live/${encodeURIComponent(sessionKey)}/recordings`
}

/** One recorded frame's JPEG, as an `<img>` / `Image()` source (same-origin, cookie auth). */
export function browserLiveFrameUrl(sessionKey: string, turnId: string, index: number): string {
  return `${browserLiveRecordingsPath(sessionKey)}/${encodeURIComponent(turnId)}/frames/${index}.jpg`
}

export function createBrowserLiveEndpoints({ get, post, j }: ClientTransport) {
  return {
    browserLive: {
      /** The card is (or is not) on screen: an off-screen card stops the screencast. */
      browserLiveVisibility: (sessionKey: string, visible: boolean) =>
        post('/api/browser/live/visibility', { session_key: sessionKey, visible, client: TAB_ID }).then(j) as Promise<BrowserLiveOk>,
      /** Frame `seq` of `stream` reached the card; the relay acks Chrome only after this.
       *  `stream` is the frame header's stream id, so a late ack for a replaced stream
       *  never releases the new stream's frame of the same number. */
      browserLiveAck: (sessionKey: string, seq: number, stream = '') =>
        post('/api/browser/live/ack', stream
          ? { session_key: sessionKey, seq, stream }
          : { session_key: sessionKey, seq }).then(j) as Promise<BrowserLiveOk>,
      /** Hand mouse and keyboard to the person; an agent op waits until they hand back. */
      browserInputClaim: (sessionKey: string) =>
        post('/api/browser/input/claim', { session_key: sessionKey }).then(j) as Promise<BrowserLiveOk>,
      /** Hand back, optionally with what the person did (`note`, their own words, at
       *  most 500 characters; `choice`, one hand-back option). `delivered` says
       *  where it went: `agent` (its waiting op takes it), `chat` (no turn is left
       *  to: the card sends it as the person's next message), or `''`. */
      browserInputRelease: (sessionKey: string, said?: BrowserHandbackSaid) =>
        post('/api/browser/input/release', { session_key: sessionKey, ...(said ?? {}) }).then(j) as Promise<BrowserLiveRelease>,
      /** One mouse, wheel or key event, in frame pixels. Never logged on either side. */
      browserInputEvent: (sessionKey: string, event: BrowserInputEvent) =>
        post('/api/browser/input/event', { session_key: sessionKey, event }).then(j) as Promise<BrowserLiveOk>,
      /** Focus the real browser window, for what the frame cannot carry (autofill, file pickers). */
      browserLiveOpenInBrowser: (sessionKey: string) =>
        post('/api/browser/live/open-in-browser', { session_key: sessionKey }).then(j) as Promise<BrowserLiveOk>,
      /** The finished card's "Open live": stream turn `turnId`'s tab again. 409 `tab_gone`
       *  when that tab is closed, `stale_turn` when a newer card owns the session. */
      browserLiveReopen: (sessionKey: string, turnId: string) =>
        post('/api/browser/live/reopen', { session_key: sessionKey, turn_id: turnId }).then(j) as Promise<BrowserLiveOk>,
      /** Answer a quick ask. Owner-only. `values` are the person's typed values and
       *  travel in this body only; the gateway refuses with `origin_changed` or
       *  `stale` when the page moved on, and fills nothing. */
      browserAskAnswer: (body: { session_key: string; ask_id: string; action: QuickAskAction; values?: QuickAskValues }) =>
        post('/api/browser/ask/answer', body).then(j) as Promise<unknown>,
      /** Fold a reopened view back to its done line. */
      browserLiveClose: (sessionKey: string) =>
        post('/api/browser/live/close', { session_key: sessionKey }).then(j) as Promise<BrowserLiveOk>,
      /** The session's recorded turns (the replay's turn picker), parsed loosely by
       *  `readRecordingList`. Resolves `null` on a 404: a gateway that keeps no
       *  recordings is a designed answer (the card replays its own frames), not a
       *  failure worth an error report. */
      browserLiveRecordings: (sessionKey: string, signal?: AbortSignal) =>
        get(browserLiveRecordingsPath(sessionKey), undefined, signal)
          .then(r => (r.status === 404 ? null : j(r))) as Promise<unknown>,
      /** One turn's frame index and action events (parsed by `readRecording`); `null` on a 404. */
      browserLiveRecording: (sessionKey: string, turnId: string, signal?: AbortSignal) =>
        get(`${browserLiveRecordingsPath(sessionKey)}/${encodeURIComponent(turnId)}`, undefined, signal)
          .then(r => (r.status === 404 ? null : j(r))) as Promise<unknown>,
    },
  }
}
