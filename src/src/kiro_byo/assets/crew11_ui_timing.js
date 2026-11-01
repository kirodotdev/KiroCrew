    // Kiro BYO client latency probe v1: observed, not provider-reported.
    // Never overwrite Crew's footer or infer elapsed from a remote timestamp.
    (function () {
      if (typeof performance === 'undefined' || typeof performance.now !== 'function' ||
          typeof document === 'undefined' || !document.addEventListener ||
          typeof window.requestAnimationFrame !== 'function') return;
      var RUN_MARK = 'data-kiro-byo-client-latency';
      var active = null, lastAction = -Infinity, shown = null;
      var pollHandle = null, MAX_MS = 30 * 60 * 1000;
      var LOCALE = {
        'zh': '界面实测', 'zh-CN': '界面实测', 'zh-TW': '介面實測', 'zh-Hant': '介面實測',
        'en': 'UI observed', 'ja': '画面で計測', 'ko': '화면 측정'
      };
      function title() {
        var lang = (document.documentElement && document.documentElement.lang) || 'zh-CN';
        return LOCALE[lang] || LOCALE[lang.split('-')[0]] || LOCALE.en;
      }
      function visible(el) {
        if (!el || !el.isConnected) return false;
        if (el.closest && el.closest('[hidden],[aria-hidden="true"],[inert]')) return false;
        if (typeof el.checkVisibility === 'function' &&
            !el.checkVisibility({checkVisibilityCSS:true})) return false;
        return !el.getClientRects || el.getClientRects().length > 0;
      }
      function live(selector) {
        return Array.from(document.querySelectorAll(selector)).filter(visible);
      }
      function context() {
        // Do not attribute a reply from a different pane to the last send.
        var inputs = live('.input-area');
        var chats = live('.chat-container');
        if (inputs.length !== 1 || chats.length !== 1 ||
            !document.querySelector('[data-testid="composer-model-chip"]')) return null;
        return {input:inputs[0], chat:chats[0]};
      }
      function messageBubbles(chat) {
        return Array.from(chat.querySelectorAll('.message-bubble')).filter(visible);
      }
      function compactStatus() {
        if (shown) return shown;
        var el = document.createElement('span');
        el.setAttribute(RUN_MARK, 'v1');
        el.setAttribute('role', 'status');
        el.setAttribute('aria-live', 'polite');
        el.style.cssText = 'position:fixed;bottom:74px;right:18px;z-index:90;'+
          'border:1px solid var(--border,#8885);background:var(--background,#222);'+
          'color:var(--foreground,inherit);border-radius:7px;padding:5px 9px;'+
          'font:12px/1.5 system-ui,sans-serif;pointer-events:none;'+
          'box-shadow:0 2px 8px #0002;max-width:240px;';
        document.body.appendChild(el);
        shown = el;
        return el;
      }
      function clearStatus() {
        if (shown && shown.parentNode) shown.parentNode.removeChild(shown);
        shown = null;
      }
      function stop() {
        if (pollHandle !== null) { window.clearTimeout(pollHandle); pollHandle = null; }
        active = null;
      }
      function sendAttempt(target) {
        var ctx = context();
        if (!ctx || !ctx.input.contains(target)) return;
        var now = performance.now();
        if (now - lastAction < 250) return; // submit plus click are one action
        lastAction = now;
        stop(); clearStatus();
        active = {
          start:now, input:ctx.input, chat:ctx.chat,
          before:new Set(messageBubbles(ctx.chat)),
          everBusy:false, idleSamples:0, requestCount:0, boundKey:null
        };
        schedule(70);
      }
      function sendControl(button) {
        if (!button || button.disabled || button.getAttribute('aria-disabled') === 'true') return false;
        if (button.type === 'submit') return true;
        var word = [button.getAttribute('data-testid'), button.getAttribute('aria-label'),
                    button.getAttribute('title')].filter(Boolean).join(' ').toLowerCase();
        // No guess from an icon-only button: might be Stop or Attach.
        return /(?:^|[\s_-])(send|发送|傳送|送信|전송)(?:$|[\s_-])/.test(word) &&
            !/(stop|cancel|停止|取消|중지)/.test(word);
      }
      document.addEventListener('submit', function(ev) {
        if (ev.defaultPrevented) return;
        var target = ev.target;
        if (target && target.closest && target.closest('.input-area')) sendAttempt(target);
      }, true);
      document.addEventListener('click', function(ev) {
        var button = ev.target && ev.target.closest && ev.target.closest('button');
        if (sendControl(button)) sendAttempt(button);
      }, true);
      document.addEventListener('keydown', function(ev) {
        if (ev.key !== 'Enter' || ev.repeat || ev.isComposing || ev.shiftKey ||
            ev.altKey || ev.defaultPrevented) return;
        var target = ev.target;
        if (!target || !target.closest || !target.closest('.input-area')) return;
        if (!target.matches('textarea,[contenteditable="true"],[role="textbox"]')) return;
        // Enter and Ctrl/Cmd+Enter are both configurable send shortcuts.
        // A newline action never yields a confirmed busy->idle Turn, so it
        // will not be reported as a measured response.
        var value = target.value === undefined ? target.textContent : target.value;
        if (!String(value || '').trim()) return;
        sendAttempt(target);
      }, true);
      function paintedFinish(pending) {
        window.requestAnimationFrame(function() {
          window.requestAnimationFrame(function() {
            if (active !== pending) return;
            if (document.visibilityState === 'hidden') { stop(); return; }
            // A final frame may navigate away from this conversation.
            var ctx = context();
            if (!ctx || ctx.chat !== pending.chat) { stop(); return; }
            var seconds = Math.max(0, (performance.now() - pending.start) / 1000);
            stop();
            // This means a *locally observed* send-to-UI-frame interval.
            // It is not an operating-system-level photon measurement.
            compactStatus().textContent = title() + ' ≈ ' + seconds.toFixed(1) + ' s';
          });
        });
      }
      function slotKey(slot) {
        var key = slot && (slot.key || slot.slot_id || slot.id || slot.session_id);
        return (typeof key === 'string' && key.length > 0) ||
          (typeof key === 'number' && Number.isFinite(key)) ? String(key) : null;
      }
      function selectedSlot(slots, pending) {
        if (!Array.isArray(slots) || slots.some(function(s) {
          return !s || typeof s.running !== 'boolean';
        })) return null;
        // An explicitly annotated chat surface is authoritative; otherwise
        // bind only a single busy slot after the user's send gesture.
        if (pending.boundKey) return slots.find(function(s) {
          return slotKey(s) === pending.boundKey;
        }) || null;
        var busy = slots.filter(function(s) { return s.running; });
        if (busy.length === 1 && slotKey(busy[0])) {
          pending.boundKey = slotKey(busy[0]);
          return busy[0];
        }
        if (slots.length === 1) return slots[0];
        return null;
      }
      function schedule(delay) {
        if (!active || pollHandle !== null) return;
        pollHandle = window.setTimeout(function() {
          pollHandle = null;
          void tick();
        }, delay);
      }
      async function tick() {
        var pending = active;
        if (!pending) return;
        if (performance.now() - pending.start > MAX_MS || context() === null ||
            document.visibilityState === 'hidden') { stop(); return; }
        var slot;
        try {
          var r = await fetch('/api/chat/slots', {cache:'no-store', credentials:'same-origin'});
          if (!r.ok) { stop(); return; }
          slot = selectedSlot(await r.json(), pending);
        } catch (_) { stop(); return; }
        if (active !== pending) return;
        if (!slot) {
          // The click may precede Crew admitting the turn to its slot list.
          // Probe briefly; never turn an unrelated idle slot into a result.
          if (!pending.everBusy && performance.now() - pending.start < 12000) {
            schedule(250); return;
          }
          stop(); return;
        }
        pending.requestCount++;
        if (slot.running || slot.compacting || slot.preparing || slot.stopping) {
          pending.everBusy = true;
          pending.idleSamples = 0;
        } else if (pending.everBusy) {
          pending.idleSamples++;
        }
        var added = messageBubbles(pending.chat).filter(function(el) {
          return !pending.before.has(el);
        });
        var assistantVisible = added.some(function(el) {
          return el.matches && el.matches('[data-role="assistant"],'+
            '[data-message-role="assistant"],[data-testid="assistant-message"],.assistant-message');
        });
        // Some Crew layouts use unlabelled user+assistant bubbles. Require
        // two NEW bubbles in that case, never mistake the user's own message
        // for a completed assistant reply.
        var answerVisible = assistantVisible || added.length >= 2;
        if (pending.everBusy && pending.idleSamples >= 1 && answerVisible) {
          paintedFinish(pending);
          return;
        }
        // Limit host API overhead during long Agent/tool runs.
        schedule(performance.now() - pending.start <= 20000 ? 250 : 800);
      }
      window.addEventListener('pagehide', function() { stop();clearStatus(); });
    })();