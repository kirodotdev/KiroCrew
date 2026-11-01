"""Display-only model name bridge for source-compatible KiroCrew 0.11 RC dashboards.

Uses Crew 0.11's model row, composer chip, SettingsSelect and SettingsMultiSelect
DOM seams. Only BYO-owned chat dropdowns hide native Auto/Jev options;
settings keep their native Auto default. Display-only labels never change
model wire IDs, dropdown click handlers, API payloads or hashed JS assets.
Installation remains transactional via integration_manager backups.
"""
from __future__ import annotations

import json
from pathlib import Path

from .crew_dashboard_patch import DashboardAssetPatch, DashboardPatchError, type_label_map
from .crew11_ui_timing import script as _client_latency_script

MARKER = 'kiro-byo:crew11-type-labels:v1'
START = '<!-- ' + MARKER + ' -->'
END = '<!-- /' + MARKER + ' -->'
PREFIX = 'kiro-byo-type:'


def display_labels() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for wire, names in type_label_map().items():
        if not wire.startswith(PREFIX):
            continue
        mode = wire[len(PREFIX):].split(':', 1)[0]
        # Prefer the product name from the pack, not its machine identifier.
        full = str(names.get('full') or '')
        mode_name = full.split(' — ', 1)[0].strip() or mode.capitalize()
        short = str(names.get('type') or '').strip()
        if not short:
            continue
        result[wire] = {'short': short, 'full': f'{mode_name} · {short}',
                        'group': str(names.get('group') or '形态 · 可手动切换')}
    return result


def _script(labels: dict[str, dict[str, str]], *, timing_asset: Path | None = None) -> str:
    # Source-controlled policy files are data, not trusted HTML/JS.
    data = json.dumps(labels, ensure_ascii=False, sort_keys=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return '''  <script id="kiro-byo-crew11-model-labels">
  (function () {
    "use strict";
    var PREFIX = "kiro-byo-type:";
    var LABELS = __LABELS__;
    var TYPE = /^kiro-byo-type:([a-z0-9_-]+):([a-z0-9_-]+)$/;
    var observer;
    var queued = false;

    function title(word) { return String(word).split(/[-_]+/).map(function(s){
      return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
    }).filter(Boolean).join(" "); }
    function label(wire) {
      var found = TYPE.exec(wire);
      if (!found) return null;
      if (LABELS[wire]) return LABELS[wire];
      var type = title(found[2]) + " Type";
      return {short: type, full: title(found[1]) + " · " + type};
    }
    // The React row's data-model-id remains the raw selection key. Only its
    // human-readable text is changed, so onSelect(m.name) stays unchanged.
    function menuLabels() {
      var spans = document.querySelectorAll('[data-model-id^="' + PREFIX + '"]');
      for (var i = 0; i < spans.length; i++) {
        var span = spans[i];
        if (!span.closest('button[role="option"], [role="option"]')) continue;
        var wire = span.getAttribute('data-model-id') || '';
        var names = label(wire);
        if (!names) continue;
        if (span.childNodes.length === 1 && span.firstChild.nodeType === 3 &&
            span.firstChild.nodeValue !== names.short) {
          // No React element replacement; the click target and selection ID
          // remain exactly as authored by Crew.
          span.firstChild.nodeValue = names.short;
        }
      }
    }
    // Presentation-only grouping for the conversational picker. React keeps its
    // original model IDs and onSelect closures. Never touch a Settings, task or
    // agent picker: the live composer + a Type row in this exact list define
    // our scope.
    function decorateConversationPickers() {
      var marked = document.querySelectorAll('[data-kiro-byo-crew11-auto], [data-kiro-byo-crew11-type-first], [data-kiro-byo-crew11-model-first]');
      for (var m = 0; m < marked.length; m++) {
        var row = marked[m];
        if (row.hasAttribute('data-kiro-byo-crew11-auto')) {
          row.removeAttribute('data-kiro-byo-crew11-auto');
          if (row.hasAttribute('data-kiro-byo-owned-inert')) { row.removeAttribute('inert'); row.removeAttribute('data-kiro-byo-owned-inert'); }
          if (row.hasAttribute('data-kiro-byo-owned-hidden')) { row.removeAttribute('aria-hidden'); row.removeAttribute('data-kiro-byo-owned-hidden'); }
        }
        row.removeAttribute('data-kiro-byo-crew11-type-first');
        row.removeAttribute('data-kiro-byo-crew11-model-first');
        row.removeAttribute('data-kiro-byo-crew11-group-label');
      }
      if (!document.querySelector('[data-testid="composer-model-chip"]')) return;
      // In this Crew version the model list is a flex column of sibling role=option
      // buttons; do not assume a generic dialog or hide Auto in other lists.
      var types = document.querySelectorAll('button[role="option"] [data-model-id^="' + PREFIX + '"]');
      var done = [];
      for (var t = 0; t < types.length; t++) {
        var firstRow = types[t].closest('button[role="option"]');
        var list = firstRow && firstRow.parentElement;
        if (!list || done.indexOf(list) >= 0) continue;
        done.push(list);
        var rows = list.querySelectorAll(':scope > button[role="option"]');
        var firstType = null, firstModel = null, hasType = false;
        for (var i = 0; i < rows.length; i++) {
          var span = rows[i].querySelector('[data-model-id]');
          if (!span) continue;
          var wire = span.getAttribute('data-model-id') || '';
          if (wire.indexOf(PREFIX) === 0) {
            hasType = true;
            if (!firstType) firstType = rows[i];
          } else if (wire === 'auto' || wire === 'auto:jev') {
            rows[i].setAttribute('data-kiro-byo-crew11-auto', '1');
            if (!rows[i].hasAttribute('inert')) { rows[i].setAttribute('inert', ''); rows[i].setAttribute('data-kiro-byo-owned-inert', '1'); }
            if (!rows[i].hasAttribute('aria-hidden')) { rows[i].setAttribute('aria-hidden', 'true'); rows[i].setAttribute('data-kiro-byo-owned-hidden', '1'); }
          } else if (hasType && !firstModel) {
            firstModel = rows[i];
          }
        }
        if (firstType) {
          var firstId = firstType.querySelector('[data-model-id]').getAttribute('data-model-id');
          var names = label(firstId);
          firstType.setAttribute('data-kiro-byo-crew11-type-first', '1');
          firstType.setAttribute('data-kiro-byo-crew11-group-label',
            (names && names.group) || '形态 · 可手动切换');
        }
        if (firstModel) firstModel.setAttribute('data-kiro-byo-crew11-model-first', '1');
      }
    }
    // Crew 0.11 Mate 2.0 uses one shared ModelField -> SimpleSelect for both
    // NewCrewmateDialog and CrewEditorModal. It has a stable uiLocation seam,
    // unlike SettingsSelect. Keep the underlying Radix option value untouched.
    // In Crew Mate model selectors show the concise Type label; the wire ID
    // and the meaningful "继承" (inherit) option remain unchanged.
    function mateLabelLeaves(root) {
      if (!root) return;
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      var n;
      while ((n = walker.nextNode())) {
        if (n.parentElement && n.parentElement.closest &&
            n.parentElement.closest('script,style,pre,code,textarea,input,[contenteditable]')) continue;
        exactMateLabel(n);
      }
    }
    function exactMateLabel(node) {
      if (!node || node.nodeType !== 3) return;
      var raw = node.nodeValue || '';
      var trimmed = raw.trim();
      var names = label(trimmed);
      if (names) node.nodeValue = raw.replace(trimmed, names.short);
    }
    function mateLabels() {
      var triggers = document.querySelectorAll('[data-ui-location="agents.model"]');
      for (var i = 0; i < triggers.length; i++) {
        var trigger = triggers[i];
        var role = trigger.getAttribute('role');
        if (role !== 'combobox' && trigger.tagName !== 'SELECT') continue;
        // Closed selection label, plus touch-native <option> text.
        mateLabelLeaves(trigger);
        if (trigger.tagName === 'SELECT') continue;
        // Radix renders choices in a body portal. Match only the listbox whose
        // ID belongs to THIS open Mate picker; never blanket-edit all portals.
        if (trigger.getAttribute('aria-expanded') !== 'true') continue;
        var listId = trigger.getAttribute('aria-controls');
        var listbox = listId && document.getElementById(listId);
        if (!listbox || listbox.getAttribute('role') !== 'listbox') continue;
        var choices = listbox.querySelectorAll('[role="option"]');
        for (var c = 0; c < choices.length; c++) mateLabelLeaves(choices[c]);
      }
    }
    // Settings -> Chat -> Model uses two separate components, not the shared
    // conversation ModelDropdownList. Keep Auto in Settings (Crew's global
    // fallback), preserve <option>.value and React's onChange model identity.
    // RC1's shipped translation keys (not arbitrary string substring matches).
    var defaultFieldNames = /^(默认模型|Default Model|Standardmodell|Modèle par défaut|Modelo predeterminado|デフォルトモデル|기본 모델|Modelo padrão|Модель по умолчанию|Modello predefinito|डिफ़ॉल्ट मॉडल|ডিফল্ট মডেল)$/i;
    var selectableFieldNames = /^(可选模型|Selectable Models|Auswählbare Modelle|Modèles sélectionnables|Modelos seleccionables|選択可能なモデル|선택 가능한 모델|Modelos selecionáveis|Модели в списке выбора|Modelli selezionabili|चुनने योग्य मॉडल|নির্বাচনযোগ্য মডেল)$/i;
    // Background model is another single-value SettingsSelect, not a
    // selectable-models checkbox list. Keep native Auto and underlying IDs.
    var backgroundFieldNames = /^(后台模型|後台模型|背景模型|Background Model|Background model|バックグラウンドモデル|백그라운드 모델)$/i;
    var settingsDefaultActive = false;
    // Some Crew RCs render the background-model field as a plain label +
    // SettingsSelect, without data-setting-label. Infer only a small field
    // with exactly one combobox; never broaden to the whole Settings page.
    function inferredBackgroundFields() {
      var labels = document.querySelectorAll('label,h3,h4,span,p');
      var fields = [];
      for (var i = 0; i < labels.length; i++) {
        if (!backgroundFieldNames.test((labels[i].textContent || '').trim())) continue;
        var parent = labels[i].parentElement;
        for (var depth = 0; depth < 4 && parent && parent !== document.body; depth++) {
          var controls = parent.querySelectorAll('[role="combobox"],select');
          if (controls.length === 1) {
            if (fields.indexOf(parent) < 0) fields.push(parent);
            break;
          }
          if (controls.length > 1) break;
          parent = parent.parentElement;
        }
      }
      return fields;
    }
    function settingField(node, re) {
      var field = node && node.closest && node.closest('[data-setting-label]');
      if (field && re.test(field.getAttribute('data-setting-label') || '')) return field;
      if (re !== backgroundFieldNames || !node) return null;
      var inferred = inferredBackgroundFields();
      for (var i = 0; i < inferred.length; i++) {
        if (inferred[i].contains(node)) return inferred[i];
      }
      return null;
    }
    function exactLabel(node, full) {
      if (!node || node.nodeType !== 3) return;
      var raw = node.nodeValue || '';
      var trimmed = raw.trim();
      var names = label(trimmed);
      if (names) node.nodeValue = raw.replace(trimmed, full ? names.full : names.short);
    }
    function settingsLabels() {
      // Collapsed default-model trigger (and native touch <select> options).
      var fields = Array.prototype.slice.call(document.querySelectorAll('[data-setting-label]'));
      var inferred = inferredBackgroundFields();
      for (var i = 0; i < inferred.length; i++) if (fields.indexOf(inferred[i]) < 0) fields.push(inferred[i]);
      for (var f = 0; f < fields.length; f++) {
        var fieldLabel = fields[f].getAttribute('data-setting-label') || '';
        if (!defaultFieldNames.test(fieldLabel) && !backgroundFieldNames.test(fieldLabel) &&
            inferred.indexOf(fields[f]) < 0) continue;
        var choices = fields[f].querySelectorAll('[role="combobox"], select, option');
        for (var c = 0; c < choices.length; c++) {
          var walker = document.createTreeWalker(choices[c], NodeFilter.SHOW_TEXT);
          var n;
          while ((n = walker.nextNode())) exactLabel(n, true);
        }
      }
      // Radix portalled default-model options: process only while its trigger
      // opened the list (not other popovers which happen to contain Type ids).
      if (settingsDefaultActive) {
        var optionRows = document.querySelectorAll('[role="listbox"] [role="option"]');
        for (var r = 0; r < optionRows.length; r++) {
          var it = document.createTreeWalker(optionRows[r], NodeFilter.SHOW_TEXT);
          var text;
          while ((text = it.nextNode())) exactLabel(text, true);
        }
      }
      // MultiSelect has native checkbox keys in an isolated role=group. Only
      // change its text/accessible name; toggling remains value-keyed in React.
      var groups = document.querySelectorAll('[role="group"][aria-label]');
      for (var g = 0; g < groups.length; g++) {
        if (!selectableFieldNames.test(groups[g].getAttribute('aria-label') || '')) continue;
        var rows = groups[g].querySelectorAll('[data-multi-select-option]');
        for (var o = 0; o < rows.length; o++) {
          var input = rows[o].querySelector('input[aria-label],button[aria-label]');
          if (input) {
            var raw = input.getAttribute('aria-label') || '';
            var names = label(raw);
            if (names) input.setAttribute('aria-label', names.full);
          }
          var spans = rows[o].querySelectorAll('span');
          for (var j = 0; j < spans.length; j++) {
            if (spans[j].childNodes.length === 1 && spans[j].firstChild.nodeType === 3)
              exactLabel(spans[j].firstChild, true);
          }
        }
      }
    }
    document.addEventListener('pointerdown', function(evt) {
      settingsDefaultActive = !!(settingField(evt.target, defaultFieldNames) ||
        settingField(evt.target, backgroundFieldNames));
      if (evt.target && evt.target.closest && evt.target.closest('[role="listbox"]'))
        cancelSelectedScroll();
      else composerTrigger(evt);
    }, true);
    document.addEventListener('focusin', function(evt) {
      if (settingField(evt.target, defaultFieldNames) ||
          settingField(evt.target, backgroundFieldNames)) settingsDefaultActive = true;
    }, true);
    function replaceWire(text, pickShort) {
      return text.replace(/kiro-byo-type:[a-z0-9_-]+:[a-z0-9_-]+/g, function(wire) {
        var names = label(wire);
        return names ? (pickShort ? names.short : names.full) : wire;
      });
    }
    // The conversation pin action is compact like the model rows, but the
    // selected chat chip and status bar intentionally use a full Type label.
    // Find the actual action (not a generic occurrence of a Type name). Do
    // not touch the model row, any option value or the action's click handler.
    var pinActionWords = /(置顶|置頂|\bpin\b|ピン留め|고정)/i;
    function pinActionLabels() {
      var chip = document.querySelector('[data-testid="composer-model-chip"]');
      if (!chip || chip.getAttribute('aria-expanded') === 'false') return;
      var typeSpan = document.querySelector('button[role="option"] [data-model-id^="' + PREFIX + '"]');
      if (!typeSpan) return;
      var typeRow = typeSpan.closest('button[role="option"]');
      var popup = typeRow && typeRow.closest('[data-radix-popper-content-wrapper],[data-side],[role="dialog"],[role="menu"]');
      // If the RC lacks an explicit portal seam, require a close common
      // ancestor with the Type list. Never scan actions across the page.
      function inSamePopup(action) {
        if (popup) return popup.contains(action);
        var parent = typeRow && typeRow.parentElement;
        for (var depth = 0; depth < 4 && parent && parent !== document.body; depth++) {
          if (parent.contains(action)) return true;
          parent = parent.parentElement;
        }
        return false;
      }
      var actions = document.querySelectorAll('button:not([role="option"]),[role="menuitem"],[role="button"]');
      for (var a = 0; a < actions.length; a++) {
        var action = actions[a];
        if (!inSamePopup(action) || !pinActionWords.test(action.textContent || '')) continue;
        if (action.closest('[data-setting-label],[data-ui-location="agents.model"]')) continue;
        var walker = document.createTreeWalker(action, NodeFilter.SHOW_TEXT);
        var node;
        while ((node = walker.nextNode())) {
          if (!node.nodeValue) continue;
          var text = node.nodeValue;
          Object.keys(LABELS).forEach(function (wire) {
            var names = label(wire);
            if (names && text.includes(names.full))
              text = text.split(names.full).join(names.short);
          });
          if (text !== node.nodeValue) node.nodeValue = text;
        }
      }
    }
    // Developer telemetry is a read-only view. Turn a *whole* wire-ID text
    // leaf into its full label only when the telemetry heading is present.
    // Never rewrite persisted metrics, JSON, logs, user messages or IDs.
    var telemetryTitles = /^(遥测|遙測|Telemetry|テレメトリー|텔레메트리)$/i;
    function telemetryLabels() {
      var headings = document.querySelectorAll('h1,h2,[role="heading"]');
      var heading = null;
      for (var i = 0; i < headings.length; i++) {
        if (telemetryTitles.test((headings[i].textContent || '').trim())) {
          heading = headings[i]; break;
        }
      }
      if (!heading) return;
      var root = heading.closest('main,[role="main"]') || document.body;
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      var node;
      while ((node = walker.nextNode())) {
        if (!safeTextNode(node)) continue;
        var raw = node.nodeValue || '';
        var trimmed = raw.trim();
        if (!TYPE.test(trimmed)) continue;
        var names = label(trimmed);
        if (names) node.nodeValue = raw.replace(trimmed, names.full);
      }
    }
    function safeTextNode(node) {
      if (!node || node.nodeType !== 3 || !node.parentElement) return false;
      var el = node.parentElement;
      if (el.closest('script,style,pre,code,textarea,input,[contenteditable],.prose,.markdown,.msg,[data-testid*="message"],[role="textbox"]')) return false;
      return true;
    }
    function visibleLabels() {
      // A live composer, status readout and model menu are only UI while the
      // BYO-selected chat is present. Never rewrite a user message or code.
      var chip = document.querySelector('[data-testid="composer-model-chip"]');
      if (chip) {
        var text = chip.querySelector('span.truncate');
        if (text && text.childNodes.length === 1 && text.firstChild.nodeType === 3) {
          var raw = text.firstChild.nodeValue || '';
          var display = replaceWire(raw, false);
          if (display !== raw) text.firstChild.nodeValue = display;
        }
        ['title', 'aria-label'].forEach(function (attr) {
          var raw = chip.getAttribute(attr) || '';
          var display = replaceWire(raw, false);
          if (display !== raw) chip.setAttribute(attr, display);
        });
      }
      // The 0.11 status bar and compact contextual model labels have no wire-id
      // data attribute. Update only exact-id text leaves, never user prose.
      if (!chip) return;
      var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      var node;
      while ((node = walker.nextNode())) {
        if (!safeTextNode(node)) continue;
        var raw = node.nodeValue || '';
        var exact = TYPE.test(raw.trim());
        if (!exact) {
          // Some Crew footer variants concatenate status and model into one
          // short text leaf. Only touch status/footer UI, never chat content.
          var el = node.parentElement;
          var rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
          var low = !!(rect && rect.bottom > 0 && rect.bottom >= window.innerHeight - 64);
          if (raw.length > 160 || !raw.includes(PREFIX) ||
              !(el.closest('footer, [role="status"], [data-testid*="status"]') || low)) continue;
        }
        if (node.parentElement.closest('[data-model-id], [role="option"]')) continue;
        var full = replaceWire(raw, false);
        if (full !== raw) node.nodeValue = full;
      }
    }
    // A model selected near the end of a long BYO catalogue must be visible
    // when the composer picker opens again. Radix/Crew can mount its portal
    // after pointerdown and can reuse a mounted portal between openings. Only
    // scroll that picker's own overflow viewport; never the page or Settings.
    var pendingSelectedScroll = false;
    var selectedScrollGeneration = 0;
    function cancelSelectedScroll() {
      pendingSelectedScroll = false;
      selectedScrollGeneration++;
    }
    function requestSelectedScroll() {
      // Crew can restore a previously cached scroll offset or render its model
      // rows *after* our initial RAF. Revalidate briefly during opening, but
      // never keep observing/repositioning once the user interacts with it.
      pendingSelectedScroll = true;
      var generation = ++selectedScrollGeneration;
      schedule();
      if (typeof window.setTimeout === 'function') {
        [60, 180, 320].forEach(function(delay, index) {
          window.setTimeout(function() {
            if (!pendingSelectedScroll || generation !== selectedScrollGeneration) return;
            alignSelectedInPicker();
            if (index === 2) cancelSelectedScroll();
          }, delay);
        });
      }
    }
    function scrollHost(row) {
      for (var el = row.parentElement, depth = 0;
           el && el !== document.body && depth < 10;
           el = el.parentElement, depth++) {
        if (el.clientHeight <= 0 || el.scrollHeight <= el.clientHeight + 2) continue;
        var style = window.getComputedStyle ? window.getComputedStyle(el) : null;
        var overflow = style && style.overflowY || '';
        if (/^(auto|scroll|overlay)$/.test(overflow) ||
            el.hasAttribute && el.hasAttribute('data-radix-scroll-area-viewport')) return el;
      }
      return null;
    }
    function selectedRow(rows, chip) {
      // Match the actual selected chip before trusting aria-selected: some
      // command-menu implementations use aria-selected for keyboard focus.
      var nameNode = chip.querySelector('span.truncate');
      var chipName = nameNode && (nameNode.textContent ||
        nameNode.firstChild && nameNode.firstChild.nodeValue || '').trim();
      var wire = chip.getAttribute('data-model-id') || '';
      var matched = null, count = 0;
      for (var i = 0; i < rows.length; i++) {
        var span = rows[i].querySelector('[data-model-id]');
        if (!span) continue;
        var id = span.getAttribute('data-model-id') || '';
        var names = label(id);
        var rowName = String(span.textContent ||
          span.firstChild && span.firstChild.nodeValue || '').trim();
        if ((wire && id === wire) || (chipName &&
            (chipName === rowName || names &&
             (chipName === names.full || chipName === names.short)))) {
          matched = rows[i]; count++;
        }
      }
      if (count === 1) return matched;
      // Only use a semantic checked state as fallback (not focus/highlight).
      for (var j = 0; j < rows.length; j++) {
        if (rows[j].getAttribute('data-state') === 'checked' ||
            rows[j].getAttribute('data-selected') === 'true') return rows[j];
      }
      return null;
    }
    function alignSelectedInPicker() {
      if (!pendingSelectedScroll) return;
      var chip = document.querySelector('[data-testid="composer-model-chip"]');
      if (!chip) return;
      // A Settings picker can coexist with an inactive composer on the page.
      if (chip.getAttribute('aria-expanded') === 'false') return;
      var types = document.querySelectorAll('button[role="option"] [data-model-id^="' + PREFIX + '"]');
      var visited = [];
      for (var t = 0; t < types.length; t++) {
        var typeRow = types[t].closest('button[role="option"]');
        var list = typeRow && typeRow.parentElement;
        if (!list || visited.indexOf(list) !== -1) continue;
        visited.push(list);
        var rows = list.querySelectorAll(':scope > button[role="option"]');
        var target = selectedRow(rows, chip);
        if (!target || !target.getBoundingClientRect) continue;
        // A cached but closed Radix portal can remain in the DOM.
        var bounds = target.getBoundingClientRect();
        if (!bounds.height) continue;
        var host = scrollHost(target);
        if (!host) continue;
        var frame = host.getBoundingClientRect();
        var maxScroll = Math.max(0, host.scrollHeight - host.clientHeight);
        // For a selected Mode Type, show the whole Type group from its top.
        // Centering Multi while it is already visible can preserve Crew's
        // previous scroll offset and cut off the group heading. Reset only
        // once on open (never during ordinary wheel/manual scrolling).
        if (target.querySelector('[data-model-id^="' + PREFIX + '"]')) {
          host.scrollTop = 0;
          // Small viewports may not fit all Types. Keep the selected Type
          // visible using the smallest necessary adjustment, not centering.
          var typeBounds = target.getBoundingClientRect();
          if (typeBounds.bottom > frame.bottom - 6)
            host.scrollTop = Math.min(maxScroll, typeBounds.bottom - frame.bottom + 6);
          return;
        }
        // Concrete models still open at their own selected row, including
        // the last entry of a long catalogue.
        if (bounds.top < frame.top + 6 || bounds.bottom > frame.bottom - 6) {
          var delta = (bounds.top + bounds.bottom - frame.top - frame.bottom) / 2;
          host.scrollTop = Math.max(0, Math.min(maxScroll, host.scrollTop + delta));
        }
        return;
      }
    }
    // Opening via pointer or keyboard may reuse the same portal: a DOM mount
    // observer alone cannot notice that. A second animation frame ensures the
    // existing group captions and list geometry are calculated before scroll.
    function composerTrigger(evt) {
      var target = evt.target;
      var chip = target && target.closest && target.closest('[data-testid="composer-model-chip"]');
      if (!chip) return;
      if (chip.getAttribute('aria-expanded') === 'true') { cancelSelectedScroll(); return; }
      if (evt.type === 'keydown' && evt.key !== 'Enter' && evt.key !== ' ') return;
      requestSelectedScroll();
    }
    document.addEventListener('keydown', function(evt) {
      if (evt.target && evt.target.closest && evt.target.closest('[role="listbox"]'))
        cancelSelectedScroll();
      else composerTrigger(evt);
    }, true);
    // Give the user control after opening; never fight manual scroll.
    ['wheel', 'touchstart'].forEach(function(eventName) {
      document.addEventListener(eventName, function(evt) {
        if (evt.target && evt.target.closest && evt.target.closest('[role="listbox"]'))
          cancelSelectedScroll();
      }, true);
    });
    function apply() {
      queued = false;
      menuLabels(); decorateConversationPickers(); mateLabels(); settingsLabels();
      pinActionLabels(); telemetryLabels(); visibleLabels();
      if (pendingSelectedScroll) (window.requestAnimationFrame || function(fn){ return window.setTimeout(fn, 0); })(alignSelectedInPicker);
    }
    function schedule() {
      if (queued) return;
      queued = true;
      (window.requestAnimationFrame || function(fn){ return window.setTimeout(fn, 0); })(apply);
    }
    function hasFullTypeName(text) {
      return Object.keys(LABELS).some(function(wire) {
        return !!(LABELS[wire] && text.includes(LABELS[wire].full));
      });
    }
    function relevant(record) {
      if (record.type === 'characterData') {
        var node = record.target;
        return !!(node.nodeValue &&
          (node.nodeValue.includes(PREFIX) || hasFullTypeName(node.nodeValue)));
      }
      if (record.type === 'attributes') {
        var el = record.target;
        var value = el.getAttribute(record.attributeName) || '';
        return value.includes(PREFIX);
      }
      for (var i = 0; i < record.addedNodes.length; i++) {
        var item = record.addedNodes[i];
        if (item.nodeType === 3 && (item.nodeValue || '').includes(PREFIX)) return true;
        if (item.nodeType === 1) {
          if ((item.textContent || '').includes(PREFIX) || hasFullTypeName(item.textContent || '')) return true;
          // Later Crew RCs may render "Sky Type" immediately, keeping the
          // namespaced wire key only in data-model-id. Observe that attribute
          // on newly mounted picker rows so their group captions are restored.
          if (item.matches && item.matches('[data-model-id^="' + PREFIX + '"]')) { requestSelectedScroll(); return true; }
          if (item.querySelector && item.querySelector('[data-model-id^="' + PREFIX + '"]')) { requestSelectedScroll(); return true; }
        }
      }
      return false;
    }
    // A mode change happens inside the BYO App, not Crew's React tree.
    // Notify Crew's normal focus/visibility refetch lifecycle instead of
    // mutating React-owned DOM rows, model IDs or the QueryClient internals.
    function onModeCatalogueChanged() {
      // Crew owns the React Query cache. This is only an advisory wake-up;
      // a standalone fetch cannot update React's stored models.
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('focus'));
      schedule();
      void checkModeCatalogue();
    }

    // The BYO App may be sandboxed in an iframe, a separate WebView, or a
    // partitioned storage context. DOM events and BroadcastChannel are not a
    // reliable transport into Crew's React Query cache. Poll the HOST's own
    // authoritative /api/models endpoint instead. This is source-constrained
    // to the Crew 0.11 dashboard; do not modify model IDs or React internals.
    var lastCatalogueKey = null;
    var pendingCatalogueKey = null;
    var modeCheckBusy = false;
    var reloadCheckBusy = false;
    function activeModeFromModels(rows) {
      if (!Array.isArray(rows)) return null;
      var found = null;
      for (var i = 0; i < rows.length; i++) {
        var key = rows[i] && (rows[i].model_name || rows[i].name);
        var match = TYPE.exec(String(key || ''));
        if (!match) continue;
        if (found && found !== match[1]) return 'mixed';
        found = match[1];
      }
      return found;
    }
    async function catalogueBackendKey(rows) {
      if (!Array.isArray(rows) || !rows.length) return null;
      var mode = activeModeFromModels(rows);
      if (mode === 'mixed') return null;
      if (mode) return 'byo:' + mode;
      // An official backend deliberately has NO BYO Type rows. Verify the
      // host's effective backend before treating the missing Type as a switch;
      // a broken BYO catalogue or a 503 must not masquerade as Kiro.
      try {
        var r = await fetch('/api/config/kirocrew', {cache:'no-store',credentials:'same-origin'});
        if (!r.ok) return null;
        var cfg = await r.json();
        var backend = cfg && cfg.agent && cfg.agent.acp_backend;
        if (typeof backend !== 'string') return null;
        return backend !== 'kiro-byo' ? 'official' : null;
      } catch (_) { return null; }
    }
    function visibleTypeMode() {
      // The live option may be a button or another ARIA option element.
      // Locate real selectable rows, never the historical transcript labels.
      var nodes = document.querySelectorAll('[data-model-id^="' + PREFIX + '"]');
      var found = null;
      for (var i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        if (typeof node.closest !== 'function' || !node.closest('[role="option"]')) continue;
        var match = TYPE.exec(node.getAttribute('data-model-id') || '');
        if (!match) continue;
        if (found && found !== match[1]) return 'mixed';
        found = match[1];
      }
      return found;
    }
    function composerSelectedKind() {
      // The closed composer chip persists even when the Radix model menu is
      // unmounted. A fresh page may already have the new server catalogue and
      // a stale restored Tiga/Dyna chip: checking the open picker alone misses
      // precisely that failure. This is READ ONLY; never replace React state.
      var chip = document.querySelector('[data-testid="composer-model-chip"]');
      if (!chip) return null;
      var wire = String(chip.getAttribute('data-model-id') || '');
      var direct = TYPE.exec(wire);
      if (direct) return 'byo:' + direct[1];
      if (wire === 'auto' || wire === 'auto:jev') return 'official';
      var content = [chip.getAttribute('title'), chip.getAttribute('aria-label'),
        chip.textContent].filter(Boolean).join(' ');
      var found = null;
      Object.keys(LABELS).forEach(function(id) {
        var entry = LABELS[id];
        if (!entry || !entry.full || content.indexOf(entry.full) < 0) return;
        var parsed = TYPE.exec(id);
        if (!parsed) return;
        if (found && found !== parsed[1]) found = 'mixed';
        else if (!found) found = parsed[1];
      });
      return found && found !== 'mixed' ? 'byo:' + found : found;
    }
    var RELOAD_ATTEMPT_KEY = 'kiro-byo:mode-refresh-attempt:v3';
    var RELOAD_RETRY_DELAY_MS = 15000;
    var RELOAD_MAX_ATTEMPTS = 2;
    function readReloadAttempt() {
      try {
        var raw = window.sessionStorage && window.sessionStorage.getItem(RELOAD_ATTEMPT_KEY);
        var value = raw && JSON.parse(raw);
        return value && typeof value === 'object' ? value : null;
      } catch (_) { return null; }
    }
    function reloadAttemptRecentlyMade(mode) {
      var saved = readReloadAttempt();
      if (!saved || saved.mode !== mode) return false;
      // Two bounded attempts allow a transiently stale React cache to settle;
      // never enter a background reload loop when the host cannot converge.
      if (Number(saved.attempts || 0) >= RELOAD_MAX_ATTEMPTS) return true;
      var age = Date.now() - Number(saved.when || 0);
      return !Number.isFinite(age) || age < RELOAD_RETRY_DELAY_MS;
    }
    function rememberReloadAttempt(mode) {
      try {
        if (!window.sessionStorage) return;
        var previous = readReloadAttempt();
        var count = previous && previous.mode === mode ? Number(previous.attempts || 0) + 1 : 1;
        window.sessionStorage.setItem(RELOAD_ATTEMPT_KEY,
          JSON.stringify({mode:mode,when:Date.now(),attempts:count}));
      } catch (_) {}
    }
    function forgetReloadAttemptWhenConverged(mode, pickerKind) {
      // A matching composer chip is NOT proof that the selectable model list
      // changed. This is the real screenshot failure: Dyna chip + Tiga picker.
      if (pickerKind !== mode) return;
      try {
        if (!window.sessionStorage) return;
        var saved = readReloadAttempt();
        if (saved && saved.mode === mode && window.sessionStorage.removeItem)
          window.sessionStorage.removeItem(RELOAD_ATTEMPT_KEY);
      } catch (_) {}
    }
    function clearObsoleteReloadAttempt(mode) {
      // Switching away from an earlier target must not poison later switches
      // back to it. The old mode's one-time guard belongs to that transition.
      try {
        var saved = readReloadAttempt();
        if (saved && saved.mode !== mode && window.sessionStorage && window.sessionStorage.removeItem)
          window.sessionStorage.removeItem(RELOAD_ATTEMPT_KEY);
      } catch (_) {}
    }
    // This banner is BYO-owned, outside React's root. It never rewrites React
    // model state; it only tells the user when a safe automatic refresh cannot
    // proceed. A one-click manual refresh is offered only as an explicit action.
    var syncBanner = null;
    var blockedReason = null;
    function setSyncBanner(key, reason) {
      blockedReason = reason;
      window.__kiroByoModeSync = {target:key || null, blocked:reason || null};
      if (!document.body) return;
      if (!key || !reason) {
        if (syncBanner && syncBanner.parentNode) syncBanner.parentNode.removeChild(syncBanner);
        syncBanner = null; return;
      }
      // No warning on an unrelated Settings/Apps page with no chat composer.
      if (!document.querySelector('[data-testid="composer-model-chip"]')) return;
      if (!syncBanner) {
        syncBanner = document.createElement('div');
        syncBanner.id = 'kiro-byo-model-sync-warning';
        syncBanner.setAttribute('role', 'status');
        syncBanner.style.cssText = 'position:fixed;top:50px;right:18px;z-index:2147483000;max-width:340px;padding:12px 14px;border-radius:10px;background:#fff8e9;color:#423519;border:1px solid #bb9b59;box-shadow:0 4px 22px #0002;font:13px/1.5 system-ui,sans-serif';
        var text = document.createElement('span');
        text.setAttribute('data-kiro-byo-sync-message', '');
        syncBanner.appendChild(text);
        var action = document.createElement('button');
        action.type = 'button';
        action.textContent = '我已确认没有运行任务，刷新聊天';
        action.style.cssText = 'display:block;margin-top:8px;cursor:pointer;border:1px solid #bb9b59;border-radius:5px;padding:4px 8px;background:#fff;color:#423519';
        action.addEventListener('click', function() { window.location.reload(); });
        syncBanner.appendChild(action);
        syncBanner.__action = action;
        document.body.appendChild(syncBanner);
      }
      var desc = reason === 'busy' ? '仍有会话执行中，结束后会自动同步。' :
        reason === 'draft' ? '检测到未发送的草稿，暂不自动刷新。' :
        reason === 'retry-limit' ? '自动刷新后菜单仍未同步，已停止重复刷新。' :
        '无法确认全部会话是否空闲，已停止自动刷新。';
      syncBanner.querySelector('[data-kiro-byo-sync-message]').textContent =
        'Kiro BYO 已切换模型目录（' + key + '），但聊天界面仍旧：' + desc;
      // Never invite a manual reload during a known live task.
      syncBanner.__action.style.display = (reason === 'busy' || reason === 'draft') ? 'none' : 'block';
    }
    async function safeToReloadCrew() {
      // The response is a verified safety contract, never inferred from a
      // model-catalogue request or a stale DOM chip. Fail closed on 403/404.
      try {
        var response = await fetch('/api/chat/slots', {cache:'no-store', credentials:'same-origin'});
        if (!response.ok) return {safe:false,reason:'slots-http-' + String(response.status || 'error')};
        var slots = await response.json();
        if (!Array.isArray(slots)) return {safe:false,reason:'slots-schema'};
        // Only *visible, editable user input* can block an automatic reload.
        // Crew retains inactive/hidden editors in the DOM (including old
        // sessions), so scanning every contenteditable falsely reports a draft
        // even when the live composer is empty. An open dialog draft remains
        // protected. Never read/print the draft itself in diagnostics.
        var editors = document.querySelectorAll('textarea,[contenteditable="true"]');
        for (var i = 0; i < editors.length; i++) {
          var editor = editors[i];
          // A focused editor may intentionally be visually transparent in a
          // rich-text composer. Never drop its draft just because it has no
          // visible rectangle: focus is stronger evidence of live input.
          var focused = document.activeElement === editor ||
              (editor.contains && document.activeElement && editor.contains(document.activeElement));
          if (!focused && (editor.hidden || (editor.closest &&
              editor.closest('[hidden],[aria-hidden="true"],[inert]')))) continue;
          // checkVisibility also catches CSS display:none / visibility:hidden
          // and opacity:0 in the real browser, including ancestors.
          if (!focused && typeof editor.checkVisibility === 'function' &&
              !editor.checkVisibility({checkVisibilityCSS:true,checkOpacity:true})) continue;
          if (!focused && typeof editor.getClientRects === 'function' &&
              editor.getClientRects().length === 0) continue;
          var draft = editor.value == null ? editor.textContent : editor.value;
          if (String(draft || '').trim()) return {safe:false,reason:'draft'};
        }
        for (var j = 0; j < slots.length; j++) {
          var slot = slots[j];
          if (!slot || !(typeof slot.running === 'boolean'))
            return {safe:false,reason:'slots-schema'};
          if (slot.running || slot.compacting || slot.preparing || slot.stopping ||
              slot.pending_approval || slot.needs_input || slot.wake_starting ||
              !Number.isFinite(Number(slot.queue_depth || 0)) || Number(slot.queue_depth || 0) !== 0)
            return {safe:false,reason:'busy'};
        }
        return {safe:true,reason:null};
      } catch (_) { return {safe:false,reason:'slots-network'}; }
    }
    async function refreshCrewIfSafe() {
      if (!pendingCatalogueKey || reloadCheckBusy || document.visibilityState === 'hidden') return;
      // Do not interrupt the BYO settings page while editing a Mode; wait until
      // the host chat composer is mounted again.
      if (document.body && !document.querySelector('[data-testid="composer-model-chip"]')) return;
      if (reloadAttemptRecentlyMade(pendingCatalogueKey)) {
        var saved = readReloadAttempt();
        if (saved && saved.mode === pendingCatalogueKey && Number(saved.attempts || 0) >= RELOAD_MAX_ATTEMPTS)
          setSyncBanner(pendingCatalogueKey, 'retry-limit');
        return;
      }
      reloadCheckBusy = true;
      try {
        var safety = await safeToReloadCrew();
        if (!safety.safe) { setSyncBanner(pendingCatalogueKey, safety.reason); return; }
        if (reloadAttemptRecentlyMade(pendingCatalogueKey)) return;
        setSyncBanner(null, null);
        // A single guarded reload remounts React Query against the same-origin
        // authoritative catalogue; never mutate its internals or model IDs.
        rememberReloadAttempt(pendingCatalogueKey);
        pendingCatalogueKey = null;
        window.location.reload();
      } finally { reloadCheckBusy = false; }
    }
    async function checkModeCatalogue() {
      if (modeCheckBusy || document.visibilityState === 'hidden') return;
      modeCheckBusy = true;
      try {
        var response = await fetch('/api/models', {cache:'no-store', credentials:'same-origin'});
        if (!response.ok) return;
        var key = await catalogueBackendKey(await response.json());
        // Refuse to guess while the API is degraded or the backend and model
        // catalogue disagree. A previous pending transition is never executed
        // on a different authoritative backend.
        if (!key) return;
        var pickerMode = visibleTypeMode();
        var pickerKey = pickerMode ? (pickerMode === 'mixed' ? 'mixed' : 'byo:' + pickerMode) : null;
        var composerKey = composerSelectedKind();
        clearObsoleteReloadAttempt(key);
        forgetReloadAttemptWhenConverged(key, pickerKey);
        if (pendingCatalogueKey && pendingCatalogueKey !== key) pendingCatalogueKey = null;
        if ((lastCatalogueKey !== null && lastCatalogueKey !== key) ||
            (pickerKey && pickerKey !== key) ||
            (composerKey && composerKey !== 'mixed' && composerKey !== key)) {
          pendingCatalogueKey = key;
          onCatalogueWake();
        }
        lastCatalogueKey = key;
        if (pendingCatalogueKey) await refreshCrewIfSafe();
        else setSyncBanner(null, null);
      } catch (_) { /* gateway may be restarting; leave the UI/session alone */ }
      finally { modeCheckBusy = false; }
    }
    function onCatalogueWake() {
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('focus'));
      schedule();
    }
    // The route is inside the host, so this works even when iframe transport
    // is broken. One probe every 3 seconds; reload is attempted ONLY for a
    // confirmed Mode difference and only when the entire chat list is idle.
    var modePoll = typeof window.setInterval === 'function' ? window.setInterval(checkModeCatalogue, 3000) : null;
    if (typeof window.addEventListener === 'function') window.addEventListener('pagehide', function() {
      if (modePoll !== null && typeof window.clearInterval === 'function') window.clearInterval(modePoll);
    });
    document.addEventListener('visibilitychange', function() {
      if (document.visibilityState !== 'hidden') void checkModeCatalogue();
    });
    if (typeof window.addEventListener === 'function') window.addEventListener('focus', function() { void checkModeCatalogue(); });
    void checkModeCatalogue();
    if (typeof window.addEventListener === 'function') window.addEventListener('kiro-byo:catalog-changed', onModeCatalogueChanged);
    try {
      var byoModeChannel = new BroadcastChannel('kiro-byo:mode-catalog');
      byoModeChannel.onmessage = function(ev) {
        if (ev.data && ev.data.kind === 'catalog-changed') onModeCatalogueChanged();
      };
    } catch (_) { /* BroadcastChannel not available in legacy WebViews */ }
    function start() {
      apply();
      observer = new MutationObserver(function(records) {
        for (var i = 0; i < records.length; i++) {
          if (!relevant(records[i])) continue;
          schedule();
          // An already-open stale picker should be checked immediately, not
          // wait for the next 3-second poll. Observe only newly mounted option
          // rows here; our own text-label decorations must not fetch-loop.
          if (records[i].type === 'childList' && visibleTypeMode())
            void checkModeCatalogue();
          break;
        }
      });
      observer.observe(document.body, {subtree:true, childList:true, characterData:true,
        attributes:true, attributeFilter:['title','aria-label','data-model-id']});
    }
    if (document.body) start();
    else document.addEventListener('DOMContentLoaded', start, {once:true});
__CLIENT_LATENCY__
  })();
  </script>
'''.replace('__LABELS__', data).replace('__CLIENT_LATENCY__', _client_latency_script(app_asset=timing_asset))


def block(labels: dict[str, dict[str, str]] | None = None, *, timing_asset: Path | None = None) -> str:
    return '  ' + START + '\n' + _script(display_labels() if labels is None else labels, timing_asset=timing_asset) + '  <style id="kiro-byo-crew11-picker-groups">\n    button[data-kiro-byo-crew11-auto="1"] { display: none !important; }\n    button[data-kiro-byo-crew11-type-first="1"],\n    button[data-kiro-byo-crew11-model-first="1"] { position: relative; }\n    button[data-kiro-byo-crew11-type-first="1"] { margin-top: 16px; }\n    button[data-kiro-byo-crew11-model-first="1"] { margin-top: 24px; }\n    button[data-kiro-byo-crew11-type-first="1"]::before,\n    button[data-kiro-byo-crew11-model-first="1"]::before {\n      content: attr(data-kiro-byo-crew11-group-label);\n      position: absolute; left: 0; right: 0; display: block;\n      pointer-events: none; text-align: left;\n      font-size: 11px; font-weight: 600; line-height: 16px; color: var(--muted, #777);\n    }\n    button[data-kiro-byo-crew11-type-first="1"]::before { top: -17px; }\n    button[data-kiro-byo-crew11-model-first="1"]::before {\n      content: "具体模型 · 固定使用";\n      top: -23px; padding-top: 4px;\n      border-top: 1px dashed var(--border-strong, var(--border, #bbb));\n    }\n  </style>\n' + '  ' + END + '\n'


def _bundle_contract(dist: Path) -> None:
    if not dist.is_dir() or not (dist / 'index.html').is_file():
        raise DashboardPatchError('Crew 0.11 dashboard index.html is missing')
    has_row = False
    has_chip = False
    has_setting_field = False
    has_selectable_row = False
    has_mate_model = False
    for asset in sorted(dist.rglob('*.js')):
        if not asset.is_file():
            continue
        source = asset.read_text(encoding='utf-8', errors='ignore')
        has_row |= 'data-model-id' in source
        has_chip |= 'composer-model-chip' in source
        has_setting_field |= 'data-setting-label' in source
        has_selectable_row |= 'data-multi-select-option' in source
        has_mate_model |= 'agents.model' in source
        if has_row and has_chip and has_setting_field and has_selectable_row and has_mate_model:
            break
    if not (has_row and has_chip and has_setting_field and has_selectable_row and has_mate_model):
        raise DashboardPatchError('Crew 0.11 model UI contract unavailable (data-model-id / composer-model-chip / data-setting-label / data-multi-select-option / agents.model)')


def discover_dashboard_patches(crew_package: Path, *, timing_asset: Path | None = None) -> list[DashboardAssetPatch]:
    dist = crew_package / 'static' / 'dist'
    _bundle_contract(dist)
    index = dist / 'index.html'
    original = index.read_text(encoding='utf-8')
    try:
        replacement = block(timing_asset=timing_asset)
    except OSError as exc:
        raise DashboardPatchError(
            'Kiro BYO dashboard resource missing or unreadable: crew11_ui_timing.js; '
            'check that the current App bundle contains src/kiro_byo/assets/crew11_ui_timing.js '
            'before updating the Crew integration'
        ) from exc
    if START in original or END in original:
        if original.count(START) != 1 or original.count(END) != 1:
            raise DashboardPatchError('Crew 0.11 BYO label guard markers are incomplete or duplicated')
        start = original.index(START)
        # Previous markers are BYO-owned and already uniqueness-checked. A
        # minified index may have no newline before the marker.
        if original[max(0, start - 2):start] == '  ':
            start -= 2
        end = original.index(END) + len(END)
        if original[end:end + 1] == '\n':
            end += 1
        patched = original[:start] + replacement + original[end:]

    else:
        import re
        heads = list(re.finditer(r'</head\s*>', original, re.IGNORECASE))
        if len(heads) != 1:
            raise DashboardPatchError('Crew 0.11 dashboard requires one </head>')
        pos = heads[0].start()
        patched = original[:pos] + replacement + original[pos:]
    return [DashboardAssetPatch(index, original, patched, original != patched)]
