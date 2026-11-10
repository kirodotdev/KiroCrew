import { AUTH_BANNER_BUTTON_CSS, AUTH_BANNER_CSS, AUTH_BANNER_INPUT_CSS, AUTH_BANNER_DISMISS_CSS } from './authBannerStyles'
import { isCameraPermissionDenied, qrScanSupported, scanQrCode } from './qrScan'
import { invalidateAcrossQueryClients } from './queryClient'
import { i18nT } from '../i18n/t'
import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'
import { X } from 'lucide-react'
import { signInCommand, signInSentence } from './signInCommand'
import type { PasteExchange } from './clientTypes'

export interface BannerContext {
  _sessionExpiredShown: boolean
  _staleOwnerBanner: boolean
  _emitAuthEvent(kind: 'mc-auth-required' | 'mc-auth-cleared' | 'mc-auth-recovered'): void
  exchangePastedToken(token: string): Promise<PasteExchange>
  removeAuthBanner(): void
  callerMayHaveChanged(): void
  callerChanged: BroadcastChannel | null
}

/**
 * Render *sentence* into *el* with the command wrapped in a <code> element.
 *
 * The command is `signInCommand()`: the catalog's `api.client.reauth_command`,
 * the SAME value a translator sees, or the one a container gateway sends, which
 * `signInSentence` put in the sentence in its place. No untranslated literal
 * sits in this module. Falls back to the plain sentence when the command is
 * absent from it, because a translation that moved or dropped it must still be
 * readable -- losing the chip is a styling regression, whereas rendering
 * nothing would be a blank banner. `ApiClient.coverage.test.tsx` pins the
 * relationship across every catalog, so a translation that breaks it reddens
 * CI rather than silently costing the chip.
 */
function setInstructionWithCommandChip(el: HTMLElement, sentence: string): void {
  const command = signInCommand()
  const at = command ? sentence.indexOf(command) : -1
  if (at < 0) {
    el.textContent = sentence
    return
  }
  el.append(document.createTextNode(sentence.slice(0, at)))
  const chip = document.createElement('code')
  chip.textContent = command
  el.append(chip, document.createTextNode(sentence.slice(at + command.length)))
}

export function renderSessionExpiredBanner(context: BannerContext, lead?: string): void {
  if (context._sessionExpiredShown) return
  context._sessionExpiredShown = true
  context._emitAuthEvent('mc-auth-required')
  const el = document.createElement('div')
  el.id = 'mc-session-expired'
  el.style.cssText = AUTH_BANNER_CSS
  const b = document.createElement('b')
  b.textContent = lead ?? i18nT('api.client.session_expired')
  const input = document.createElement('input')
  input.type = 'text'
  input.placeholder = i18nT('api.client.paste_token_url_or_raw_token')
  input.style.cssText = AUTH_BANNER_INPUT_CSS
  input.addEventListener('focus', () => { input.style.borderColor = '#fff'; input.style.boxShadow = '0 0 0 3px rgba(255,255,255,0.25),0 0 20px rgba(255,255,255,0.1)' })
  input.addEventListener('blur', () => { input.style.borderColor = '#fca5a5'; input.style.boxShadow = 'none' })
  // A refused paste has to say so. The exchange's only other cue is the field
  // re-enabling, which is indistinguishable from nothing having happened, so
  // without this the user presses Enter again and concludes the banner is
  // broken. `role="status"` announces the text when it appears, since a sighted
  // user sees it arrive and a screen-reader user otherwise would not.
  //
  // A `div`, and deliberately unstyled: block layout puts it on its own line
  // without an inline-spacing rule, and it inherits the banner's white-on-red
  // type, which clears contrast at this size where a lighter red would not.
  const failure = document.createElement('div')
  failure.setAttribute('role', 'status')
  /**
   * Exchange one sign-in URL or raw token, from the paste field or the QR scan.
   *
   * Both entry points share this ONE body on purpose: the parse, the exchange, the
   * refusal wording, the owner-latch rule and the query invalidation below are
   * single-sourced, so a scan cannot drift from a paste. Answers null when *raw*
   * carried nothing to send (a URL with no `token`), without touching the screen:
   * the paste path treats that as a no-op, and the scan path words it, since a
   * scanned code that is not a sign-in code needs an answer. Otherwise answers the
   * exchange that settles the attempt.
   *
   * One exchange at a time. A second submit while one is in flight gets the running
   * exchange back and sends nothing: the first answer is the answer, and two
   * concurrent exchanges would race their refusal text and their banner teardown.
   * The disabled field and button make that rare, not impossible, since a scan can
   * decode while a paste is still out.
   */
  let exchange: Promise<void> | null = null
  const submitToken = (raw: string): Promise<void> | null => {
    let t: string | null = null
    try { t = new URL(raw).searchParams.get('token') } catch { t = raw }
    if (!t) return null
    if (exchange) return exchange
    // Disabled for the round trip so a second Enter cannot start a second
    // exchange. The pasted text is left in the field: on a refusal the user
    // corrects it rather than re-pasting from scratch.
    input.disabled = true
    // Drop the previous attempt's refusal now, so the next one is visibly a
    // new answer rather than text that was already on screen.
    failure.textContent = ''
    exchange = context.exchangePastedToken(t).then(({ reached, ok, tokenAccepted, ownerOk }) => {
      input.disabled = false
      if (!reached) {
        // The request never got an answer, so nothing judged the token. Saying
        // it was not accepted would assert a check that never ran and send the
        // user to re-run a command that cannot help.
        failure.textContent = i18nT('api.client.token_exchange_unreachable')
        input.focus()
        return
      }
      if (!ok) {
        failure.textContent = signInSentence(i18nT('api.client.token_not_accepted'))
        input.focus()
        return
      }
      // An owner denial is resolved by ONE event: a credential that both
      // authenticates AND clears the owner gate. A 2xx here establishes
      // neither. `/api/auth/me` is not owner-gated, so this session -- still
      // authenticated, still owner-denied -- answers 200 on its cookie, and
      // the middleware quietly falls back to that cookie when the pasted token
      // is invalid. A token minted before the owner was configured is valid
      // too, so it is accepted and still denied everywhere the gate fronts.
      // Clearing the latch on the status alone, or on acceptance alone, would
      // hide the prompt while every owner-gated call kept failing, and the
      // user would learn that from their next refused save.
      //
      // So the latch drops only when the gateway reports both. Without them
      // the banner stays up and says the token was not accepted, which is the
      // accurate reading of a 200 that proves neither.
      if (context._staleOwnerBanner && !(tokenAccepted && ownerOk)) {
        failure.textContent = signInSentence(i18nT('api.client.token_not_accepted'))
        input.focus()
        return
      }
      // `removeAuthBanner` returns early while the latch is set --
      // deliberately, because that session keeps answering 2xx on everything
      // the owner gate does not front, so an unrelated success proves
      // nothing. Dropping it here is what lets the banner clear on its own
      // recovery, which the previous full-page reload hid by wiping the
      // document.
      context._staleOwnerBanner = false
      // Auth works again. `removeAuthBanner` drops the banner and clears the
      // terminal-refresh latch, so a LATER lapse in this document retries the
      // silent path instead of going straight back to the banner.
      context.removeAuthBanner()
      // Only queries that failed AND are holding nothing get refetched.
      //
      // `status === 'error'` alone is not enough, and the earlier version of
      // this comment was wrong to say an error-state query has no data to sync
      // from. React Query KEEPS the last successful `data` when a later
      // refetch fails: measured against `@tanstack/query-core`, a query that
      // succeeds and then fails a refetch reports `status: 'error'` with its
      // `data` still defined and unchanged. Refetching one of those
      // re-delivers data to whatever watches it, and
      // `McpCustomServerModal`'s effect runs
      // `setText(JSON.stringify(specQuery.data.spec, ...))` on every change of
      // `specQuery.data` -- so an unsaved spec draft would be silently
      // overwritten by the server's copy. A no-argument
      // `invalidateQueries()` does that to every panel at once, which is the
      // wider version of the same bug.
      //
      // `data === undefined` narrows it to queries that never carried a
      // successful value: exactly the ones the lapse broke, and the only ones
      // with nothing to overwrite a draft with. The token may name another
      // caller, so the refusals kept for the old one go first, here and in
      // every other tab that shares the cookie.
      context.callerMayHaveChanged()
      context.callerChanged?.postMessage(null)
      invalidateAcrossQueryClients({
        predicate: (q) => q.state.status === 'error' && q.state.data === undefined,
      })
    }).finally(() => {
      exchange = null
    })
    return exchange
  }
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const v = input.value.trim()
      if (!v) return
      void submitToken(v)
    }
  })
  // The connective text around the command used to sit here as two bare English
  // It used to be two bare English fragments wrapped around a <code> element,
  // which left the banner untranslated everywhere while the panel's own error
  // card was translated. A key per fragment is not the fix: the i18n gate
  // rejects a value that ends mid-sentence, because the translator cannot
  // reorder around a sibling it never sees -- and several languages need the
  // command somewhere English does not put it. `api.client.
  // session_expired_sign_in_again` already carries this same command inline in a
  // whole sentence across every locale, so this follows that precedent. A `div`
  // so it needs no inline spacing rule.
  //
  // The command still gets its own <code> element, found by splitting the
  // rendered sentence on the command itself rather than by a placeholder: that
  // keeps one whole translatable sentence in one key AND keeps the command
  // visually separable, which is the whole reason a reader can tell where it
  // begins and ends.
  const instruction = document.createElement('div')
  setInstructionWithCommandChip(
    instruction,
    signInSentence(i18nT('api.client.run_kirocrew_token_then_paste_sign_in_url')),
  )
  el.append(b, instruction, input, failure)
  // On a phone the desktop "Connect your phone" QR carries this same sign-in URL,
  // but the camera app opens it in the DEFAULT browser, which restores a session
  // in the wrong browser when the dashboard runs in another one. Scanning in the
  // page feeds the decoded string into `submitToken`, so the session is restored
  // in this tab. Appended only once the browser proves it can decode `qr_code`:
  // the check is async, and a browser without the API (iOS WebKit today) gets
  // the paste field alone.
  void qrScanSupported().then((supported) => {
    if (!supported || !el.isConnected) return
    const scan = document.createElement('button')
    scan.type = 'button'
    scan.textContent = i18nT('api.client.scan_qr_code')
    scan.style.cssText = AUTH_BANNER_BUTTON_CSS
    scan.addEventListener('click', () => {
      failure.textContent = ''
      // `alive` ties the camera to the banner: a 2xx elsewhere, or the dismiss,
      // removes `el`, and the next frame stops every track and closes the overlay.
      //
      // The button is not disabled while the viewfinder is up: the overlay makes
      // the rest of the page inert, which already keeps a second tap off it, and
      // an enabled button is what the viewfinder can hand focus back to on close.
      // It is disabled for the exchange that follows a decode, until that settles,
      // so a decode cannot start a second exchange over the first one.
      void scanQrCode({ alive: () => el.isConnected }).then(
        (raw) => {
          if (raw === null) return
          // The decoded string is never logged: in the intended use it IS the token.
          const settled = submitToken(raw.trim())
          if (!settled) {
            failure.textContent = signInSentence(i18nT('api.client.token_not_accepted'))
            return
          }
          scan.disabled = true
          void settled.finally(() => { scan.disabled = false })
        },
        (err: unknown) => {
          // The paste field stays usable either way: the camera is an addition,
          // and whatever stopped it does not stop a paste.
          failure.textContent = isCameraPermissionDenied(err)
            ? i18nT('api.client.camera_permission_denied')
            : i18nT('api.client.camera_unavailable')
        },
      )
    })
    input.after(scan)
  })
  const dismiss = document.createElement('button')
  dismiss.setAttribute('aria-label', i18nT('app.dismiss'))
  // flushSync so the icon is in the DOM when this returns, like the rest of the banner.
  flushSync(() => createRoot(dismiss).render(createElement(X, { className: 'lucide-inline' })))
  dismiss.style.cssText = AUTH_BANNER_DISMISS_CSS
  dismiss.addEventListener('click', () => {
    el.remove()
    context._sessionExpiredShown = false
    context._staleOwnerBanner = false
    context._emitAuthEvent('mc-auth-cleared')
  })
  el.append(dismiss)
  document.body.prepend(el)
  requestAnimationFrame(() => input.focus())
}
