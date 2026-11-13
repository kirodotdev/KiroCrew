// The in-iframe height-reporter script, split out of widgetSrcdoc.ts so that
// file stays under the repo's 2,000-line cap. This is the reporter and only the
// reporter: its tuning constants and the script body. widgetSrcdoc.ts imports
// HEIGHT_REPORTER_BODY and injects it (via textContent) into the sandboxed
// document when `includeHeightReporter` is set.
//
// The script runs INSIDE the null-origin sandbox. It carries no LLM/user
// content: the only interpolation below is the numeric tuning constants in this
// module, so it is a static trusted string. It posts `{type:'mc-widget-height',
// height}` to the parent, which sizes the frame to the reported height.
//
// It is shared by every surface that auto-sizes a frame to its content:
// WidgetFrame (inline chat widgets and the popout), ArtifactBody (the
// full-screen artifact viewer) and the library thumbnails. A change here
// changes all of them, which is why the viewport-feedback guard below is
// written to leave ordinary content sizing untouched.

import { MIN_REPORTED_FRAME_HEIGHT, MAX_REPORTED_FRAME_HEIGHT } from '../utils/widgetHeights'

/** Sub-threshold height jitter (px) the in-iframe reporter ignores. Animated
 * widgets (canvas, CSS transitions) reflow the body by a pixel or two every
 * frame; reporting those would flood the parent with postMessages. */
const HEIGHT_REPORT_EPSILON_PX = 2
/** A smaller height must hold this long (ms) before the iframe reports a
 * shrink. A reload / Tailwind JIT reflow / animation frame briefly reports a
 * smaller height before the content settles; an animated widget that bounces
 * between a low and high value keeps cancelling this timer, so the shrink
 * never fires and the reporter goes quiet on the high value. */
const HEIGHT_REPORT_SHRINK_MS = 200
/** How long the document may stay taller than the frame's viewport, with the
 * viewport height unchanged, before the reporter gives the frame back its own
 * vertical scroll. A self-sizing host grows the frame to a reported height
 * within a frame or two, and every viewport resize restarts this window, so
 * only a frame held at a fixed height (expanded view, popout, thumbnail)
 * outlasts it. */
const HEIGHT_REPORT_FIT_GRACE_MS = 300
/** How far (px) the rounded-up content height may exceed the frame's viewport
 * and still count as fitting. A frame's CSS height is a whole pixel, but its
 * viewport can come out up to a pixel shorter once the engine snaps it to
 * device pixels at a fractional display scale, and clientHeight rounds that
 * value either way. Without this slack a self-sizing frame sized to its exact
 * report reads as too short, gets its scrollbar back, and the loop the
 * overflow handling exists to break resumes. */
const HEIGHT_REPORT_FIT_SLACK_PX = 1

/** How many consecutive NON-CONVERGENT growth reports mark a viewport-feedback
 * loop (see the guard in the reporter body).
 *
 * A `vh`-sized element feeds the frame: a frame of viewport V reports a content
 * height whose EXCESS over the viewport, e = content - V, decides whether the
 * frame settles.
 *   - `calc(100vh - Npx)` on content taller than N: content = V + k for a
 *     positive constant k (the injected body padding plus whatever sits beside
 *     the vh element). The host grows the frame to V + k, the viewport becomes
 *     V + k, the next report is V + 2k, and the excess e = k stays CONSTANT
 *     while the frame climbs to the MAX clamp -- it never settles.
 *   - `height:98vh` (any multiplier below 1): content = 0.98 V + c, so
 *     e = c - 0.02 V SHRINKS as the viewport grows and reaches a fixed point
 *     (the frame settles on its own). This is convergent and must follow
 *     normally -- pinning it would cut it off.
 *   - a normal tall page / `min-height:100vh`: the content height does not
 *     grow with the viewport at all (e falls as the frame catches up), so it
 *     settles too.
 * The guard therefore arms ONLY when the excess stays flat or grows across a
 * streak (non-convergent). A convergent layout's shrinking excess never
 * reaches the streak, and neither does a one-off reflow. */
const HEIGHT_RUNAWAY_STREAK = 4
/** Tolerance (px) for the excess-trend and pin-coordinate comparisons. Covers
 * sub-pixel rounding across the frame / viewport / content round-trip; well
 * under any real one-off expansion. */
const HEIGHT_RUNAWAY_STEP_SLACK_PX = 4
/** The viewport must grow at least this far, with the excess (content minus
 * viewport) staying flat the whole way, before a loop is called.
 *
 * This is what tells divergent feedback from a slowly-convergent layout. A
 * `calc(100vh - Npx)` loop holds a CONSTANT excess no matter how far the frame
 * climbs (slope 0). A `height:Mvh` layout with M below 1 loses 1-M of the
 * viewport's growth from its excess each step -- `98vh` sheds 0.02px of excess
 * per px of viewport growth -- so over this much growth its excess falls well
 * past the slack and the streak breaks before a pin is ever armed, letting it
 * converge to its own fixed point. Chosen so (1 - 0.99) * GROWTH comfortably
 * exceeds the slack: even a `99vh` layout's excess drops > SLACK across it. */
const HEIGHT_RUNAWAY_MIN_GROWTH_PX = 600
/** How long (ms) a provisional pin waits for the host to apply it before giving
 * up. The guard posts a one-screen pin and expects the host to shrink the frame
 * to it; a host that does not size itself to reports at all (or ignores this
 * one) must not leave the reporter stuck, so after this long with no viewport
 * movement the guard drops back to normal following and posts the real height.
 * Longer than one deferred-shrink window (HEIGHT_REPORT_SHRINK_MS) so a host
 * that merely defers the shrink is not mistaken for one that ignores it. */
const HEIGHT_RUNAWAY_PENDING_MS = 500
/** How long (ms) the guard stops posting before it posts a pin, to see whether
 * the content grows on its own. A viewport-feedback loop changes height only
 * when the viewport does, so with no new report the frame and the content both
 * stand still. Steady self-growth (rows streaming in, an accordion opening)
 * keeps changing while the viewport stands still, which rules it out with no
 * shrink, so the frame never dips to one screen and back. */
const HEIGHT_RUNAWAY_HOLD_MS = 100
/** How far (px) the guard shrinks a frame stuck at the MAX clamp to test it.
 * There the viewport cannot grow, so the streak never sees the content follow
 * it; a small shrink does: viewport-fed content shrinks with it, a static
 * document taller than the clamp does not and goes straight back. Small, so a
 * static document only loses this much at its far bottom for a moment. */
const HEIGHT_RUNAWAY_CLAMP_PROBE_PX = 64

/** Body of the height-reporter script. Defined as a string literal -- the only
 * interpolation is the numeric tuning constants above (never LLM/user content;
 * the LLM `html` never reaches here). Set as a script element via textContent
 * in buildSrcdoc; the iframe re-parses and executes it via the standard
 * <script> mechanism.
 *
 * The reporter is deliberately quiet: it coalesces ResizeObserver bursts into
 * one measurement per animation frame, ignores sub-pixel jitter, applies growth
 * eagerly, and defers/cancels shrinks. The net effect is that a continuously
 * animating widget (lava lamp, starry night) reports its height once and then
 * stops posting, instead of feeding a per-frame resize loop back to the parent.
 * The one deliberate exception is the window `load` re-report below: quietness
 * must never extend across a load event the parent can observe, because parent
 * surfaces treat post-load silence as the frame no longer showing this document.
 *
 * It also hides the frame's own vertical scrollbar while the frame holds the
 * whole document. A classic (space-taking) scrollbar is the one thing that
 * feeds the frame's size back into the content's height: a frame that is a
 * fraction of a pixel short of its content shows a scrollbar, the narrower
 * viewport re-wraps the text taller, the frame grows, the scrollbar goes, the
 * text re-wraps shorter, the frame shrinks, and the cycle repeats every half
 * second. With the vertical overflow hidden while everything fits, the width
 * the content lays out at does not depend on the frame's height, so the height
 * settles. Once the document stays taller than a viewport that is not resizing
 * (see HEIGHT_REPORT_FIT_GRACE_MS) the scroll comes back, so a fixed-height
 * frame keeps scrolling its content.
 *
 * VIEWPORT-FEEDBACK GUARD. A `vh`-sized element on content taller than the
 * slice it subtracts has no fixed point: each height report is viewport + k for
 * a positive constant k, the host grows the frame to it, and the frame climbs k
 * px per report to the 100,000px clamp (see HEIGHT_RUNAWAY_STREAK). The guard
 * watches the EXCESS of each growth report over the current viewport: a flat or
 * growing excess across a streak is non-convergent feedback, which it stops by
 * posting a one-screen pin and holding it; a shrinking excess is a convergent
 * layout (`98vh`, a normal tall page) and is followed normally. The pin is
 * provisional until the host applies it (bounded by HEIGHT_RUNAWAY_PENDING_MS),
 * and once applied the "moved away from the pin" test is done in the SAME
 * scrollbar-excluding coordinate the pin produced, so a horizontal scrollbar
 * cannot be read as an external resize. */
export const HEIGHT_REPORTER_BODY = `(function(){
  var EPS = ${HEIGHT_REPORT_EPSILON_PX};
  var SHRINK_MS = ${HEIGHT_REPORT_SHRINK_MS};
  var FIT_GRACE_MS = ${HEIGHT_REPORT_FIT_GRACE_MS};
  var FIT_SLACK = ${HEIGHT_REPORT_FIT_SLACK_PX};
  var RUNAWAY_STREAK = ${HEIGHT_RUNAWAY_STREAK};
  var RUNAWAY_SLACK = ${HEIGHT_RUNAWAY_STEP_SLACK_PX};
  var RUNAWAY_MIN_GROWTH = ${HEIGHT_RUNAWAY_MIN_GROWTH_PX};
  var RUNAWAY_PENDING_MS = ${HEIGHT_RUNAWAY_PENDING_MS};
  var RUNAWAY_HOLD_MS = ${HEIGHT_RUNAWAY_HOLD_MS};
  var CLAMP_PROBE = ${HEIGHT_RUNAWAY_CLAMP_PROBE_PX};
  var RUNAWAY_MIN_PIN = ${MIN_REPORTED_FRAME_HEIGHT};
  var RUNAWAY_MAX_PIN = ${MAX_REPORTED_FRAME_HEIGHT};
  var lastSent = -1;      // last height actually posted to the parent
  var baseViewH = -1;     // the frame's viewport at the first report
  var runawayStreak = 0;  // consecutive flat-excess growth reports
  var streakStartExcess = -1; // the excess (content - viewport) when the streak began
  var streakStartViewH = -1;  // the viewport when the streak began, for the growth span
  // Feedback-pin state. 'none': following reports normally. 'holding': a
  // non-convergent streak tripped (or the frame overflows at the MAX clamp);
  // we stopped posting for RUNAWAY_HOLD_MS to see whether the content grows on
  // its own. 'pending': the hold passed, we posted a one-screen pin and are waiting
  // for the host to apply it (bounded by RUNAWAY_PENDING_MS). 'confirmed': the
  // host applied the pin and the frame now holds one screen; further
  // viewport-tracking growth is suppressed until a real resize.
  var pinState = 'none';
  var pinTargetH = -1;      // the one-screen height we posted as the pin
  var pinnedClientH = -1;   // clientHeight after the host applied the pin (its own coords)
  var pendingArmedAt = 0;   // Date.now() when the pin was posted, for the bound
  var pendingBaseViewH = -1;// viewport when the pin was posted, to see the host apply it
  var prePinMeasure = -1;   // content height when the pin was posted, for the confirm
  var loopRuledOut = false; // this document proved it is NOT a runaway this load; never re-arm
  var holdH = -1;           // content height when the hold began (refreshed if the viewport moves)
  var holdViewH = -1;       // viewport when the hold began (refreshed if the host still resizes)
  var holdTimer = null;
  var lastEvalViewH = -1;   // viewport at the previous measurement
  var holdAtClamp = false;  // the hold is for a frame stuck at the MAX clamp
  var probeH = -1;          // content height when the clamp probe was posted
  var shrinkTimer = null;
  var rafId = 0;
  var raf = window.requestAnimationFrame || function(cb){ return setTimeout(cb, 16); };
  var unraf = window.cancelAnimationFrame || clearTimeout;
  var now = function(){ return (window.Date && Date.now) ? Date.now() : (new Date()).getTime(); };
  var root = document.documentElement;
  // The document's own inline overflow-y, restored whenever the frame scrolls.
  var authoredOverflowY = root.style.overflowY;
  var clipped = false;
  // Set when the grace window hands the scroll back; holds until the viewport
  // changes height.
  var released = false;
  var fitTimer = null;
  var viewH = root.clientHeight;
  function send(h){
    lastSent = h;
    parent.postMessage({type:'mc-widget-height', height:h}, '*');
  }
  function measure(){
    // The bottom of body's margin box, from the top of the document, rounded
    // UP to a whole pixel. A frame sized to a rounded-down height is short of
    // its content by a fraction of a pixel, which is enough to overflow.
    //
    // Measured on body, NOT documentElement: documentElement.scrollHeight is at
    // least the iframe's own viewport height, so once the iframe grows it can
    // never report smaller, ratcheting the frame taller and taller (capped at
    // MAX_HEIGHT) and never shrinking back to the actual content. body is
    // content-sized (no height), so its box tracks the real content and can
    // shrink. The larger of its box and its scrollHeight covers content that
    // overflows the box (absolutely positioned children, a fixed body height);
    // its top offset covers its own margin, a child margin collapsed through
    // its top edge, and any padding on html.
    var b = document.body;
    var r = b.getBoundingClientRect();
    var top = r.top + (window.scrollY || 0);
    var marginBottom = parseFloat(window.getComputedStyle(b).marginBottom) || 0;
    return Math.ceil(top + Math.max(r.height, b.scrollHeight) + marginBottom);
  }
  function setClipped(on){
    if (on === clipped) return;
    clipped = on;
    root.style.overflowY = on ? 'hidden' : authoredOverflowY;
  }
  function pinTarget(){
    // ONE SCREEN, measured for real, not guessed. baseViewH is the frame's
    // viewport at its first report -- the host's cached or estimated start
    // height -- which can be a tiny estimate (DEFAULT_WIDGET_HEIGHT) or a cached
    // runaway height (27,000+ for anyone who already hit this bug), so it is NOT
    // a reliable one-screen value and is used only as a last resort. The device
    // screen height (screen.availHeight) IS one screen, so prefer it, clamped to
    // the same MIN/MAX bounds the parent applies to every reported height. Only
    // when it cannot be read do we fall back to the start viewport.
    var avail = 0;
    try { avail = (window.screen && window.screen.availHeight) || 0; } catch (e) {}
    var pin = avail > 0 ? avail : (baseViewH > 0 ? baseViewH : root.clientHeight);
    if (pin < RUNAWAY_MIN_PIN) pin = RUNAWAY_MIN_PIN;
    if (pin > RUNAWAY_MAX_PIN) pin = RUNAWAY_MAX_PIN;
    return pin;
  }
  function resetPin(){
    if (holdTimer){ clearTimeout(holdTimer); holdTimer = null; }
    pinState = 'none';
    pinTargetH = -1;
    pinnedClientH = -1;
    runawayStreak = 0;
    streakStartExcess = -1;
  }
  function armPin(h){
    // Post the provisional one-screen pin (see the 'pending' branch in evaluate).
    var pin = pinTarget();
    if (!(pin > 0 && pin < lastSent)){ resetPin(); return; }
    pinState = 'pending';
    pinTargetH = pin;
    pendingArmedAt = now();
    pendingBaseViewH = root.clientHeight;
    prePinMeasure = h;
    send(pin);
  }
  function startHold(h, atClamp){
    // Stop posting for RUNAWAY_HOLD_MS. A feedback loop then stands still; if
    // the content still changes while the viewport stands still, it is growing
    // on its own and evaluate() drops the hold with no shrink posted.
    pinState = 'holding';
    holdAtClamp = atClamp;
    holdH = h;
    holdViewH = root.clientHeight;
    if (holdTimer) clearTimeout(holdTimer);
    holdTimer = setTimeout(function(){
      holdTimer = null;
      if (pinState !== 'holding') return;
      var cur = measure();
      if (Math.abs(root.clientHeight - holdViewH) <= EPS && Math.abs(cur - holdH) > EPS){
        resetPin();
        schedule();
        return;
      }
      if (holdAtClamp){
        // At the clamp the content was never seen to follow the viewport, so
        // test that with a small shrink before any one-screen pin.
        pinState = 'probing';
        probeH = cur;
        pinTargetH = root.clientHeight - CLAMP_PROBE;  // re-posted by a load re-report
        pendingArmedAt = now();
        pendingBaseViewH = root.clientHeight;
        send(pinTargetH);
        return;
      }
      armPin(cur);
    }, RUNAWAY_HOLD_MS);
  }
  function overflowsAtClamp(h){
    // A frame already within RUNAWAY_MIN_GROWTH of the MAX clamp (a cached
    // runaway height) cannot grow far enough for the streak to arm, and the host
    // clamps every report back, so the deadband swallows it for good. Content
    // still taller than that frame goes straight to the hold and pin check.
    var ch = root.clientHeight;
    return pinState === 'none' && !loopRuledOut && ch >= RUNAWAY_MAX_PIN - RUNAWAY_MIN_GROWTH
      && h > ch + EPS && pinTarget() < lastSent;
  }
  function fits(h){
    // clientHeight on the root element is the viewport height less any
    // scrollbar, so a horizontal scrollbar never counts as room.
    return h <= root.clientHeight + FIT_SLACK;
  }
  function fit(h){
    if (fits(h)){
      if (fitTimer){ clearTimeout(fitTimer); fitTimer = null; }
      // Once the scroll is back, a fit while the scrollbar shows is not a fit
      // at full width: content that keeps its aspect ratio gets shorter when
      // the scrollbar narrows it, and clipping again would widen it past the
      // viewport and toggle the scrollbar forever. Only a viewport resize
      // (see the resize handler) clears the latch.
      if (!released) setClipped(true);
      return;
    }
    if (!clipped || fitTimer) return;
    // Taller than the frame. A self-sizing host is about to grow it; give the
    // scroll back only if the viewport has not resized by the time this fires.
    fitTimer = setTimeout(function(){
      fitTimer = null;
      if (!fits(measure())){ released = true; setClipped(false); }
    }, FIT_GRACE_MS);
  }
  function evaluate(){
    rafId = 0;
    // Whether the viewport moved since the previous measurement. A feedback
    // loop's content grows in the same layout pass as the resize our own post
    // caused; content that grows on its own after a pause (rows loading) grows
    // in a later measurement, with the viewport standing still.
    var viewMoved = root.clientHeight !== lastEvalViewH;
    lastEvalViewH = root.clientHeight;
    var h = measure();
    fit(h);
    if (lastSent < 0){
      if (baseViewH < 0) baseViewH = root.clientHeight;
      // A load re-report (lastSent reset) while a pin is in force must re-post
      // the PIN (or the clamp probe), not the runaway content height -- for
      // 'pending' and 'probing' as well as 'confirmed'. The window load event can fire while the host is still
      // holding its deferred shrink to our pin; posting the full height here
      // would cancel that shrink, the provisional pin would time out, the loop
      // would be ruled out, and the runaway would resume. Re-posting pinTargetH
      // keeps the host's shrink target consistent so the pin still lands.
      var armed = (pinState === 'confirmed' || pinState === 'pending' || pinState === 'probing')
        && pinTargetH > 0;
      send(armed ? pinTargetH : h);
      if (!armed && overflowsAtClamp(h)) startHold(h, true);
      return;
    }            // first measurement
    if (pinState === 'holding'){
      var hv = root.clientHeight;
      if (Math.abs(hv - holdViewH) > EPS){
        // The host is still applying an earlier report; content that follows
        // it proves nothing either way. Re-base and keep holding.
        holdH = h;
        holdViewH = hv;
        return;
      }
      if (Math.abs(h - holdH) <= EPS) return;  // standing still: still looks like a loop
      // The content changed while the viewport stood still: not viewport
      // feedback. Drop the hold (no pin was posted, so the frame never dipped)
      // and handle this measurement normally below.
      resetPin();
    }
    if (pinState === 'probing'){
      // We shrank a clamped frame by CLAMP_PROBE. Content that shrank with it
      // is fed by the viewport: pin it to one screen. Content that did not is
      // a static document taller than the clamp: give it the clamp back and
      // never probe it again this load.
      var probeMoved = pendingBaseViewH - root.clientHeight;
      if (probeMoved > EPS){
        if (probeH - h > RUNAWAY_SLACK) armPin(h);
        else { resetPin(); loopRuledOut = true; send(h); }
        return;
      }
      if (now() - pendingArmedAt > RUNAWAY_PENDING_MS){
        resetPin();
        loopRuledOut = true;
        send(h);
      }
      return;
    }
    if (pinState === 'pending' && pendingBaseViewH - root.clientHeight <= EPS
        && h > prePinMeasure + RUNAWAY_SLACK){
      // The content grew while the host had not yet applied the pin (a host
      // that defers its shrink). A feedback loop cannot grow with a still
      // viewport, so drop the pin and post the real height; that cancels the
      // host's deferred shrink before the frame dips.
      resetPin();
      send(h);
      return;
    }
    if (pinState === 'pending'){
      // We posted a one-screen pin; wait for the host to apply it. "Applied"
      // means the viewport has moved toward the pin (it shrank from its pre-pin
      // value). The host may defer the shrink (WidgetFrame holds it 250ms), so
      // judge by movement, not by a fixed delay.
      var moved = pendingBaseViewH - root.clientHeight;
      if (moved > EPS){
        // The host sized the frame to the pin. CONFIRM the loop only if the
        // content ALSO followed the viewport down -- a vh-sized element does
        // (content ~ viewport + k, so shrinking the viewport shrinks the
        // content), while steady self-growth (an accordion opening, streamed
        // rows) ignores the viewport and keeps climbing. The non-convergent
        // excess that armed the pin does not, by itself, tell those apart -- a
        // constant-step self-growth shows the same flat excess -- so this
        // second gate is what rejects it.
        //
        // Following only part of the way still counts: min-height:calc(100vh
        // - N) over a fixed content floor drops to that floor and no further,
        // yet it runs away just the same once followed. Only content that did
        // not move down at all is ruled out.
        var followed = prePinMeasure - h;
        if (followed > RUNAWAY_SLACK){
          // Record the resulting clientHeight in the HOST's own
          // scrollbar-excluding coordinates: a horizontal scrollbar makes
          // clientHeight a dozen-odd px below the frame's CSS height, and the
          // resize handler must compare against THIS value, not the pin, or it
          // would read the scrollbar gap as an external resize and clear the
          // pin on every frame.
          pinnedClientH = root.clientHeight;
          pinState = 'confirmed';
          return;
        }
        // Content did not follow the viewport down: not a loop. Resume normal
        // following and rule this document out for the load so the same growth
        // does not re-arm the pin and dip the frame again.
        resetPin();
        loopRuledOut = true;
        if (h > lastSent) send(h);
        return;
      }
      if (now() - pendingArmedAt > RUNAWAY_PENDING_MS){
        // The host never applied the pin (it does not size itself to reports, or
        // ignores this one). Do not stay stuck: resume normal following, post
        // the real height, and rule this document out for the load so we do not
        // re-arm on the same growth every few frames.
        resetPin();
        loopRuledOut = true;
        send(h);
        return;
      }
      return;  // still waiting, within the bound
    }
    if (Math.abs(h - lastSent) <= EPS){              // within deadband -- ignore jitter
      if (shrinkTimer){ clearTimeout(shrinkTimer); shrinkTimer = null; }
      if (overflowsAtClamp(h)) startHold(h, true);
      return;
    }
    if (h > lastSent){
      // Growth applies eagerly so genuine expansion (streaming text, opening a
      // section) isn't laggy. Cancel any pending shrink: an animated widget
      // that oscillates low<->high bounces back here every frame, so the shrink
      // below never commits and we stop posting once locked to the high value.
      if (shrinkTimer){ clearTimeout(shrinkTimer); shrinkTimer = null; }
      if (pinState === 'confirmed') return;  // held at one screen until a real resize
      // Non-convergent-feedback guard. The excess of the content over the
      // current viewport tells a runaway loop from a layout that settles:
      //   - a calc(100vh - N) loop holds a CONSTANT excess no matter how far
      //     the frame climbs, so it never settles -> pin it;
      //   - a height:Mvh (M < 1) or a normal tall page sheds excess as the
      //     viewport grows and reaches a fixed point -> follow it.
      // One step cannot tell them apart (a slow converger's per-step shed is
      // sub-pixel), so measure the excess across the WHOLE streak: keep the
      // streak only while the excess stays within RUNAWAY_SLACK of where the
      // streak began, and arm only once the viewport has grown RUNAWAY_MIN_GROWTH
      // with that excess still flat. A converger's excess falls past the slack
      // within that growth and resets the streak before any pin.
      var excess = h - root.clientHeight;
      // Any excess above the reporting deadband can climb (each step past EPS
      // is posted and grows the frame), so it may start a streak; RUNAWAY_SLACK
      // is only the tolerance for comparing excesses across the streak.
      // Growth with the viewport standing still is the content's own and never
      // counts toward a loop.
      if (loopRuledOut || excess <= EPS || !viewMoved){
        runawayStreak = 0;
        streakStartExcess = -1;
      } else if (runawayStreak === 0){
        runawayStreak = 1;              // begin a streak
        streakStartExcess = excess;
        streakStartViewH = root.clientHeight;
      } else if (excess >= streakStartExcess - RUNAWAY_SLACK){
        runawayStreak++;                // excess still flat: loop still plausible
        var grew = root.clientHeight - streakStartViewH;
        if (runawayStreak >= RUNAWAY_STREAK && grew >= RUNAWAY_MIN_GROWTH){
          var pin = pinTarget();
          if (pin > 0 && pin < lastSent){
            // Hold before pinning: this growth is not posted, so a feedback
            // loop stands still and self-growth shows itself (see startHold).
            startHold(h, false);
            return;
          }
          // Pin not below the current height (nothing to gain): keep watching
          // from here rather than re-counting the same span.
          runawayStreak = 1;
          streakStartExcess = excess;
          streakStartViewH = root.clientHeight;
        }
      } else {
        // Excess fell past the slack: convergent. Restart the observation from
        // the new, lower excess rather than giving up -- a layout that is still
        // genuinely diverging will hold its (new) excess flat from here.
        runawayStreak = 1;
        streakStartExcess = excess;
        streakStartViewH = root.clientHeight;
      }
      send(h);
    } else {
      // Shrink: defer until the smaller height has held for SHRINK_MS. Reset on
      // every tick so a continuously oscillating widget never fires it.
      if (shrinkTimer) clearTimeout(shrinkTimer);
      shrinkTimer = setTimeout(function(){
        shrinkTimer = null;
        var cur = measure();
        if (cur < lastSent && Math.abs(cur - lastSent) > EPS) send(cur);
      }, SHRINK_MS);
    }
  }
  function schedule(){
    // Coalesce a burst of ResizeObserver callbacks (an animation can fire many
    // per frame) into a single measurement per frame.
    //
    // Cancel-and-reschedule rather than an early return on a pending handle:
    // requestAnimationFrame may return a handle whose callback never runs (a
    // frame queued for a page the browser then puts in the back/forward cache is
    // dropped), and latching on such a handle would stop this widget's height
    // tracking permanently. Same defect and same fix as CliPanel's theme
    // scheduler. NOTE this block lives inside a template literal, so it must
    // carry neither a backtick nor a dollar-brace interpolation opener -- both
    // terminate the literal, and the resulting parse error points here rather
    // than at the string, which is why this warning is worth the two lines.
    if (rafId) unraf(rafId);
    rafId = raf(evaluate);
  }
  new ResizeObserver(schedule).observe(document.body);
  window.addEventListener('resize', function(){
    // A frame that changes height is being sized by its host: restart the grace
    // window so a frame catching up with a growth never gets its scrollbar back.
    // A height-only resize leaves body's box unchanged, so the ResizeObserver
    // above does not see it; re-measure here to re-check fit.
    var h = root.clientHeight;
    if (h !== viewH){
      viewH = h;
      released = false;
      // Clear the feedback pin only on a REAL external resize. While confirmed,
      // the frame sits at the pin and clientHeight reads pinnedClientH (the
      // pin's own scrollbar-excluding coordinate, recorded when the host applied
      // it); a horizontal scrollbar appearing or the frame settling leaves
      // clientHeight at that value, so judging against pinnedClientH -- not the
      // pin -- means a scrollbar gap is never mistaken for a resize. Only a
      // viewport that moved off pinnedClientH by more than the slack is the user
      // resizing the window, and only then do we reconsider (and take the new
      // viewport as the fresh base -- never a viewport our own pin produced).
      if (pinState === 'confirmed' && Math.abs(h - pinnedClientH) > RUNAWAY_SLACK){
        resetPin();
        baseViewH = h;
        loopRuledOut = false;
      }
      if (fitTimer){ clearTimeout(fitTimer); fitTimer = null; }
    }
    schedule();
  });
  window.addEventListener('load', function(){
    // Re-report UNCONDITIONALLY after load, bypassing the deadband. The parent
    // surface re-arms its silence window on EVERY iframe load event (an engine
    // renavigation onto a spent single-use url fires load with no reporter
    // behind it, and silence within the grace window is its only signal -- see
    // DOC_REPORT_GRACE_MS in ArtifactBody). A first measurement that raced ahead
    // of the load event -- layout settles before images and fonts finish --
    // would otherwise be the LAST post this document ever makes: the parent
    // discards it at load, the deadband swallows this re-check because the
    // height is unchanged, and three seconds later a healthy, rendering document
    // is flagged as no longer showing. Resetting lastSent routes the scheduled
    // measurement through the first-measurement path, so every load the parent
    // can observe is followed by a report.
    setTimeout(function(){ lastSent = -1; schedule(); }, 100);
  });
  // Start without a vertical scrollbar, so a frame still growing to its first
  // report never flashes one; fit() hands the scroll back if it stays short.
  setClipped(true);
  schedule();
  document.addEventListener('click', function(e){
 // NOTE: this isTrusted check runs INSIDE the sandboxed iframe
    // and is NOT the security trust boundary. LLM-emitted <script> in this same
    // document can call parent.postMessage({type:'mc-widget-action',...})
    // directly and skip this handler entirely. The real protection is on the
    // parent side: a widget action only PRE-FILLS the composer (it never
    // auto-submits a user-role turn). Do not treat this guard as authoritative.
    if (!e.isTrusted) return;
    var el = e.target.closest('[data-action]');
    if (!el) return;
    e.preventDefault();
    var action = el.dataset.action;
    var payload = {};
    try { payload = JSON.parse(el.dataset.payload || '{}'); } catch(x){}
    var inputs = document.querySelectorAll('input,select,textarea');
    var formData = {};
    inputs.forEach(function(inp){
      var n = inp.name || inp.id || inp.getAttribute('data-field');
      if (!n) return;
      if (inp.type === 'checkbox') formData[n] = inp.checked;
      else if (inp.type === 'radio') { if (inp.checked) formData[n] = inp.value; }
      else formData[n] = inp.value;
    });
    if (Object.keys(formData).length) payload.formData = formData;
    parent.postMessage({type:'mc-widget-action', action:action, payload:payload}, '*');
  });
})();`
