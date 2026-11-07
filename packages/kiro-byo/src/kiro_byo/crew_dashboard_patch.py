"""Dashboard integration for KiroCrew 0.10's shared model catalogue.

KiroCrew deliberately gives every model picker one React-Query catalogue.  The
Kiro BYO integration must therefore never vary ``/api/models`` by page or force
a page reload just to rebuild that cache.  BYO exposes one stable catalogue
shape while it is the active harness: Mode-owned Type sentinels followed by the
concrete provider models.  Chat may use the Type entries as routing selections;
non-Chat surfaces keep KiroCrew's native SPA lifecycle and the DOM guard hides
those virtual rows there. Scheduled-task editors opened over Chat also count as
non-Chat surfaces even when the route remains /chat. Some Insider editors lack
role=dialog, so detection must inspect scheduling form controls locally.

The integration intentionally does **not** patch KiroCrew's compiled
``withJevRoute`` helper. Vite/Rollup is free to inline, split, lower or rename
that helper differently between release builds, so treating a minified callable
shape as a compatibility contract is brittle.  Instead we rely on KiroCrew's
authored ``data-model-id`` DOM contract plus its existing slot-model API.

No Kiro BYO code calls ``window.location.reload`` and no route transition changes
the model fetcher.  This keeps Chat, Settings, Agents and the rest of the
dashboard SPA-native on Windows, macOS and Linux.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TYPE_PREFIX = "kiro-byo-type:"

# Legacy markers/functions are retained for deterministic migration tests and to
# recognize old Kiro BYO bundles, but production discovery no longer depends on
# their minified expression shapes.
AUTO_MARKER = "/*kiro-byo:no-native-auto*/"
JEV_MARKER = "/*kiro-byo:no-jev-route*/"

HTML_GUARD_MARKER = "kiro-byo:model-picker-guard"
HTML_GUARD_ID = "kiro-byo-model-picker-guard"


class DashboardPatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class DashboardAssetPatch:
    path: Path
    original: str
    patched: str
    changed: bool


# --- Legacy minified-JS patch helpers -------------------------------------
# Kept only so an old patched asset can still be reasoned about in tests and
# recovery tooling. New installs use the HTML/CSS guard below.
_AUTO_RETURN_RE = re.compile(
    r"return\s*\[\s*\{\s*\.\.\.(?P<live>[A-Za-z_$][\w$]*)\s*,\s*"
    r"name\s*:\s*['\"]auto['\"]\s*,\s*description\s*:\s*['\"]['\"]\s*\}\s*,\s*"
    r"\.\.\.(?P<rest>[A-Za-z_$][\w$]*)\s*\]"
)
_JEV_DIRECT_RE = re.compile(
    r"return\s*\[\s*\{\s*name\s*:\s*['\"]auto:jev['\"]\s*,\s*"
    r"description\s*:\s*(?P<desc>[A-Za-z_$][\w$]*)\s*\}\s*,\s*"
    r"\.\.\.(?P<models>[A-Za-z_$][\w$]*)\s*\]"
)
_JEV_CONST_RE = re.compile(r"(?P<const>[A-Za-z_$][\w$]*)\s*=\s*['\"]auto:jev['\"]")


def _type_test(array_name: str) -> str:
    return (
        f'{array_name}.some(_kb=>typeof _kb.name==="string"&&'
        f'_kb.name.startsWith("{TYPE_PREFIX}"))'
    )


def _patch_auto(text: str) -> tuple[str, bool]:
    if AUTO_MARKER in text:
        return text, False
    matches = list(_AUTO_RETURN_RE.finditer(text))
    if len(matches) != 1:
        raise DashboardPatchError(
            f"expected exactly one KiroCrew withAutoFirst return expression, found {len(matches)}"
        )
    match = matches[0]
    live = match.group("live")
    rest = match.group("rest")
    replacement = (
        f"return {_type_test(rest)}?{rest}:"
        f'[{{...{live},name:"auto",description:""}},...{rest}]{AUTO_MARKER}'
    )
    return text[: match.start()] + replacement + text[match.end() :], True


def _patch_jev(text: str) -> tuple[str, bool]:
    if JEV_MARKER in text:
        return text, False

    direct = list(_JEV_DIRECT_RE.finditer(text))
    if len(direct) == 1:
        match = direct[0]
        desc = match.group("desc")
        models = match.group("models")
        replacement = (
            f"return {_type_test(models)}?{models}:"
            f'[{{name:"auto:jev",description:{desc}}},...{models}]{JEV_MARKER}'
        )
        return text[: match.start()] + replacement + text[match.end() :], True
    if len(direct) > 1:
        raise DashboardPatchError(
            f"ambiguous Auto(Jev) inline expression: found {len(direct)} candidates"
        )

    consts = [m.group("const") for m in _JEV_CONST_RE.finditer(text)]
    for const in dict.fromkeys(consts):
        pattern = re.compile(
            r"return\s*\[\s*\{\s*name\s*:\s*"
            + re.escape(const)
            + r"\s*,\s*description\s*:\s*(?P<desc>[A-Za-z_$][\w$]*)\s*\}\s*,\s*"
            r"\.\.\.(?P<models>[A-Za-z_$][\w$]*)\s*\]"
        )
        matches = list(pattern.finditer(text))
        if len(matches) == 1:
            match = matches[0]
            desc = match.group("desc")
            models = match.group("models")
            replacement = (
                f"return {_type_test(models)}?{models}:"
                f'[{{name:{const},description:{desc}}},...{models}]{JEV_MARKER}'
            )
            return text[: match.start()] + replacement + text[match.end() :], True
        if len(matches) > 1:
            raise DashboardPatchError(
                f"ambiguous Auto(Jev) expression for compiled constant {const}: "
                f"found {len(matches)} candidates"
            )

    raise DashboardPatchError("KiroCrew Auto(Jev) route expression was not found")


def patch_bundle_text(text: str) -> str:
    """Legacy helper retained for old-bundle recovery/unit tests."""
    text, _ = _patch_auto(text)
    text, _ = _patch_jev(text)
    return text


# --- Production HTML guard -------------------------------------------------
# The selectors intentionally operate on one ModelDropdownList container.  The
# presence of a BYO Type row is therefore the backend discriminator; switching
# back to Kiro's official backend makes the rule inert without another patch.
_GUARD_STYLE = f'''\n  <style id="{HTML_GUARD_ID}">\n    /* {HTML_GUARD_MARKER} */
    /* A chat catalogue can remain in React Query briefly after SPA navigation.
       Scheduled-task editors may also portal over the still-live Chat route.
       Neither location allows Type sentinels. The stale marker covers cached
       model ids rendered as plain text instead of data-model-id. */
    html:not(.kiro-byo-chat-surface) [data-model-id^="{TYPE_PREFIX}"],
    html:not(.kiro-byo-chat-surface) [data-kiro-byo-stale-type-row="1"],
    html.kiro-byo-task-editor-open [data-model-id^="{TYPE_PREFIX}"],
    html.kiro-byo-task-editor-open [data-kiro-byo-stale-type-row="1"],
    html.kiro-byo-task-editor-open [data-kiro-byo-task-type-row="1"],
    html:not(.kiro-byo-chat-surface) [data-kiro-byo-task-type-row="1"],
    html:not(.kiro-byo-chat-surface) [role="option"]:has([data-model-id^="{TYPE_PREFIX}"]),
    html.kiro-byo-task-editor-open [role="option"]:has([data-model-id^="{TYPE_PREFIX}"]) {{
      display: none !important;
    }}
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) div:has(> button [data-model-id^="{TYPE_PREFIX}"]) > button:has([data-model-id="auto"]),
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) div:has(> button [data-model-id^="{TYPE_PREFIX}"]) > button:has([data-model-id="auto:jev"]),
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) [data-kiro-byo-native-auto="1"] {{
      display: none !important;
    }}
    /* Keep the picker width/scrollbar allocation stable while Crew refreshes
       per-selection capability/context metadata. */
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) [role="listbox"]:has([data-model-id^="{TYPE_PREFIX}"]),
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) div:has(> button [data-model-id^="{TYPE_PREFIX}"]) {{
      scrollbar-gutter: stable;
    }}
    /* Mode-owned Types are dynamic. The injected script annotates each Type\n       row from its namespaced wire id, so this CSS never needs to know whether\n       the active Mode is Tiga, Dyna, Gaia, or a user-supplied pack. */\n    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) [data-model-id^="{TYPE_PREFIX}"] {{\n      font-size: 0 !important;\n    }}\n    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) [data-model-id^="{TYPE_PREFIX}"]::after {{\n      content: attr(data-kiro-byo-type-label);\n      font-size: 13px;\n    }}\n    /* Keep group captions outside the option's painted box. Crew paints the
       selected/hover background on the button itself, so captions rendered in
       the button content get highlighted together with the first row. Put the
       captions in the button's margin instead. */
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) button[role="option"][data-kiro-byo-type-first="1"],
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) button[role="option"][data-kiro-byo-model-first="1"] {{
      position: relative;
    }}
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) button[role="option"][data-kiro-byo-type-first="1"] {{
      margin-top: 24px;
    }}
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) button[role="option"][data-kiro-byo-model-first="1"] {{
      margin-top: 34px;
    }}
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) button[role="option"][data-kiro-byo-type-first="1"]::before,
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) button[role="option"][data-kiro-byo-model-first="1"]::before {{
      position: absolute;
      box-sizing: border-box;
      left: 0;
      right: 0;
      display: block;
      pointer-events: none;
      font-family: inherit;
      font-size: 11px;
      font-weight: 600;
      line-height: 16px;
      color: var(--muted);
      text-align: left;
    }}
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) button[role="option"][data-kiro-byo-type-first="1"]::before {{
      content: attr(data-kiro-byo-group-label);
      top: -20px;
    }}
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) button[role="option"][data-kiro-byo-model-first="1"]::before {{
      content: "具体模型 · 固定使用";
      top: -30px;
      border-top: 1px dashed var(--border-strong, var(--border));
      padding-top: 8px;
    }}
    /* KiroCrew caches selection capabilities by slot, not by the selected model.
       During a concrete-model -> Type switch the previous model's effort=true
       answer can remain visible until the refetch settles. The selected picker
       row is authoritative for presentation, so fail closed for Type rows only.
       Concrete models remain entirely under Crew's native effort UI. */
    html.kiro-byo-chat-surface:not(.kiro-byo-task-editor-open) [data-kiro-byo-type-active="1"] [data-kiro-byo-effort-block="1"] {{
      display: none !important;
    }}
  </style>\n'''


# Crew also prints the raw model wire id outside the picker list: the composer
# model chip / status bar and the "pin <model> to <agent>" row. The script
# rewrites only the visible text of Kiro BYO Type ids. Selection identity stays
# namespaced on the wire as ``kiro-byo-type:<mode>:<type>``.
GUARD_END = "<!-- kiro-byo-guard-end -->"
LABEL_SEPARATOR = " — "


def _type_names(display_name: str, type_id: str) -> tuple[str, str]:
    """Return ``(short_name, picker_label)`` for one Mode-local Type.

    The compact status chip intentionally stays ``<Mode> — <Type>`` while
    picker rows and Type-specific actions use the explicit ``<Type> Type``
    product label. Packs may provide any display name; when ``Type`` is
    omitted we add it only to the picker/action label.
    """
    name = (display_name or "").strip() or type_id[:1].upper() + type_id[1:]
    if name.lower().endswith(" type"):
        short = name[:-5].strip()
        picker = name
    else:
        short = name
        picker = f"{name} Type"
    return short or type_id, picker


def type_label_map() -> dict[str, dict[str, str]]:
    """Return metadata keyed by the fully namespaced Type wire id.

    Mode-local Type ids may overlap freely (for example both Dyna and Gaia may
    define ``power``) because the key contains the owning Mode. Installed pack
    metadata provides exact product labels; the browser script also has a
    deterministic title-cased fallback for packs added after installation.
    """
    try:
        from .policy_packs import load_policy_packs
        from .protocol import type_selection_id

        packs = load_policy_packs()
    except Exception:  # pragma: no cover - label map is cosmetic only
        return {}
    labels: dict[str, dict[str, str]] = {}
    for pack in packs:
        if not getattr(pack, "enabled", True):
            continue  # never decorate an unavailable mode as an executable Type
        mode = (pack.short_name or pack.display_name or pack.id).strip()
        default_policy_id = str(getattr(pack, "default_policy", "") or "").strip()
        default_policy = next((row for row in pack.policies if row.id == default_policy_id), None)
        if default_policy is not None:
            default_name = (getattr(default_policy, "localized_name", "") or _type_names(default_policy.display_name, default_policy.id)[0]).strip()
            group_label = f"形态 · 默认{default_name}，可手动切换"
        else:  # parser normally prevents this; cosmetic fallback remains safe.
            group_label = "形态 · 可手动切换"
        for policy in pack.policies:
            type_name, picker_label = _type_names(policy.display_name, policy.id)
            wire = type_selection_id(pack.id, policy.id)
            labels[wire] = {
                "full": f"{mode}{LABEL_SEPARATOR}{type_name}",
                "type": picker_label,
                "group": group_label,
            }
    return dict(sorted(labels.items()))


def _label_script(labels: dict[str, dict[str, str]]) -> str:
    import json

    table = json.dumps(labels, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    return f'''  <script id="kiro-byo-model-label">
    (function () {{
      var PREFIX = "{TYPE_PREFIX}";
      var CHAT_CLASS = "kiro-byo-chat-surface";
      var TASK_EDITOR_CLASS = "kiro-byo-task-editor-open";
      var STALE_ROW = "data-kiro-byo-stale-type-row";
      var _decorateFrame = 0;

      function normalizedPath() {{
        var path = String(window.location.pathname || "");
        while (path.length > 1 && path.endsWith("/")) path = path.slice(0, -1);
        return path;
      }}
      function isInteractiveChatPath(path) {{
        /* KiroCrew's conversation route is /chat/<name>. Settings -> Chat also
           contains a literal "chat" path segment on Insider builds, so the old
           broad chat-segment test falsely classified Settings as the live
           conversation surface and injected Type rows into global defaults. */
        var parts = String(path || "").split("/").filter(Boolean);
        var chatIndex = parts.indexOf("chat");
        if (chatIndex < 0) return false;
        if (parts.indexOf("settings") >= 0) return false;
        return true;
      }}
      function hasScheduledFormControls(root) {{
        if (!root || !root.querySelectorAll) return false;
        /* Static control captions, not task titles/messages, identify a
           schedule form. KiroCrew Insider can portal its editor in a plain
           div without role=dialog or aria-modal. */
        var text = String(root.textContent || "");
        var frequency = /Cron\\s*(?:表达式|expression)|任务的运行频率|此任务的运行频率|task\\s+(?:run\\s+)?frequency/i.test(text);
        var timezone = /时区|time\\s*zone|タイムゾーン|시간대/i.test(text);
        var model = /模型|model|代理|agent|エージェント|모델/i.test(text);
        return frequency && timezone && model &&
          root.querySelectorAll('input,textarea,select,[role="combobox"]').length >= 2;
      }}
      function isScheduleTaskEditorOpen() {{
        if (!document.body || !document.querySelectorAll) return false;
        var dialogs = document.querySelectorAll('[role="dialog"], dialog, [aria-modal="true"]');
        for (var i = 0; i < dialogs.length; i++) {{
          if (hasScheduledFormControls(dialogs[i])) return true;
        }}
        /* Fallback for the KiroCrew scheduled-task editor without accessible
           dialog attributes. Start from its *short, static Cron label* and
           require the neighbouring timezone/model controls in the same local
           form subtree, never scan conversation or code text as a task form. */
        var leaves = document.querySelectorAll('label,span,small,div,button,p');
        for (var n = 0; n < leaves.length; n++) {{
          var el = leaves[n];
          if (el.childElementCount ||
              (el.closest && el.closest('.prose,.msg,pre,code,textarea,[contenteditable]'))) continue;
          var caption = String(el.textContent || '').trim();
          if (!/^(?:Cron\\s*(?:表达式|expression)|任务的运行频率|此任务的运行频率|task\\s+(?:run\\s+)?frequency)$/i.test(caption)) continue;
          var parent = el.parentElement;
          for (var depth = 0; parent && parent !== document.body && depth < 12; depth++, parent = parent.parentElement) {{
            if (hasScheduledFormControls(parent)) return true;
          }}
        }}
        return false;
      }}
      function syncTaskEditorSurface() {{
        var root = document.documentElement;
        var active = isScheduleTaskEditorOpen();
        var changed = root.classList.contains(TASK_EDITOR_CLASS) !== active;
        if (changed) root.classList.toggle(TASK_EDITOR_CLASS, active);
        return changed;
      }}
      function isChatSurface() {{
        return document.documentElement.classList.contains(CHAT_CLASS) &&
          !document.documentElement.classList.contains(TASK_EDITOR_CLASS);
      }}
      /* KiroCrew 0.10 intentionally shares one React-Query model catalogue
         across every picker.  Keep that invariant: route changes only toggle
         presentation state; they never change the fetch shape and never reload
         the dashboard. */
      var _surfaceIsChat = isInteractiveChatPath(normalizedPath());
      document.documentElement.classList.toggle(CHAT_CLASS, _surfaceIsChat);
      function syncSurface() {{
        var overlayChanged = syncTaskEditorSurface();
        var after = isInteractiveChatPath(normalizedPath());
        if (after === _surfaceIsChat && !overlayChanged) return;
        _surfaceIsChat = after;
        document.documentElement.classList.toggle(CHAT_CLASS, after);
        if (document.body) {{
          walk(document.body);
          scheduleDecorate(document);
        }}
      }}
      var _push = history.pushState, _replace = history.replaceState;
      history.pushState = function () {{ var r = _push.apply(this, arguments); syncSurface(); return r; }};
      history.replaceState = function () {{ var r = _replace.apply(this, arguments); syncSurface(); return r; }};
      window.addEventListener("popstate", syncSurface);

      var LABELS = {table};
      var RE = /kiro-byo-type:([a-z0-9_-]+):([a-z0-9_-]+)/g;
      function pretty(value) {{
        return String(value || "").split(/[-_]+/).filter(Boolean).map(function (part) {{
          return part.charAt(0).toUpperCase() + part.slice(1);
        }}).join(" ");
      }}
      function meta(wire) {{
        var known = LABELS[wire];
        if (known) return known;
        var raw = wire.slice(PREFIX.length).split(":");
        var mode = pretty(raw[0]);
        var type = pretty(raw[1]);
        return {{ full: mode + "{LABEL_SEPARATOR}" + type, type: type + " Type", group: "形态 · 可手动切换" }};
      }}
      function isPinAction(node) {{
        var el = node.parentElement;
        if (!el) return false;
        var row = el.closest('button, [role="menuitem"], [role="option"], li') || el;
        var text = String(row.textContent || "");
        return /置顶|固定|pin(?:ned)?|ピン/i.test(text) ||
          (/\bdefault\b/i.test(text) && /代理|agent/i.test(text));
      }}
      function replacementFor(node, wire) {{
        var value = meta(wire);
        return isPinAction(node) ? value.type : value.full;
      }}
      function skip(node) {{
        var el = node.parentElement;
        return !el || !!el.closest("[data-model-id], .msg, .prose, pre, code, textarea, [contenteditable]");
      }}
      function semanticRowFor(node) {{
        var el = node && node.parentElement;
        if (!el || !el.closest) return null;
        var row = el.closest('button[role="option"], [role="option"], [role="menuitem"], li, label');
        if (row) return row;
        var cur = el;
        for (var depth = 0; cur && depth < 5; depth++, cur = cur.parentElement) {{
          if (cur.querySelector && cur.querySelector('input[type="checkbox"], input[type="radio"]')) return cur;
        }}
        return null;
      }}
      function markStaleTypeRow(node) {{
        var row = semanticRowFor(node);
        if (row && row.getAttribute(STALE_ROW) !== "1") row.setAttribute(STALE_ROW, "1");
        return row;
      }}
      function clearStaleTypeRow(node) {{
        var row = semanticRowFor(node);
        if (row && row.hasAttribute(STALE_ROW)) row.removeAttribute(STALE_ROW);
      }}
      function fixText(node) {{
        var v = node.nodeValue;
        if (!v || v.indexOf(PREFIX) < 0 || skip(node)) return;
        if (!isChatSurface()) {{
          markStaleTypeRow(node);
          return;
        }}
        clearStaleTypeRow(node);
        var nv = v.replace(RE, function (wire) {{ return replacementFor(node, wire); }});
        if (nv !== v) node.nodeValue = nv;
      }}
      function setAttr(el, name, value) {{
        if (el && el.getAttribute(name) !== value) el.setAttribute(name, value);
      }}
      function setFlag(el, name, enabled) {{
        if (!el) return;
        if (enabled) setAttr(el, name, "1");
        else if (el.hasAttribute(name)) el.removeAttribute(name);
      }}
      function findEffortBlock(dialog) {{
        if (!dialog || !dialog.querySelector) return null;
        var range = dialog.querySelector('input[type="range"]');
        var node = range;
        while (node && node !== dialog) {{
          var parent = node.parentElement;
          if (parent && parent.querySelector && parent.querySelector(':scope > [role="listbox"]')) return node;
          node = parent;
        }}
        return null;
      }}
      function syncTypeEffortBoundary(container) {{
        if (!container || !container.closest) return;
        var dialog = container.closest('[role="dialog"]');
        if (!dialog) return;
        var selectedType = container.querySelector(
          ':scope > button[role="option"][aria-selected="true"] [data-model-id^="' + PREFIX + '"]'
        );
        setFlag(dialog, 'data-kiro-byo-type-active', !!selectedType);
        var effortBlock = findEffortBlock(dialog);
        if (effortBlock) setAttr(effortBlock, 'data-kiro-byo-effort-block', '1');
      }}
      function markPlainTaskTypeRows() {{
        /* Some KiroCrew editors render human-friendly Type labels without
           data-model-id. Restrict the presentation to the actual conversation
           picker. Restore tagged rows when navigation returns to Chat. */
        var active = !isChatSurface();
        var old = document.querySelectorAll('[data-kiro-byo-task-type-row="1"]');
        for (var i = 0; i < old.length; i++) {{
          if (!active) {{
            old[i].removeAttribute('data-kiro-byo-task-type-row');
            if (old[i].getAttribute('data-kiro-byo-task-type-inert') === '1') {{
              old[i].removeAttribute('inert');
              old[i].removeAttribute('data-kiro-byo-task-type-inert');
            }}
          }}
        }}
        if (!active) return;
        var options = document.querySelectorAll('button,[role="option"],[role="menuitem"],li');
        for (var j = 0; j < options.length; j++) {{
          var row = options[j];
          if (row.querySelector('[data-model-id]')) continue;
          if (!isKnownTypeLabel(String(row.textContent || '').trim())) continue;
          setAttr(row, 'data-kiro-byo-task-type-row', '1');
          if (!row.hasAttribute('inert')) {{
            row.setAttribute('inert', '');
            setAttr(row, 'data-kiro-byo-task-type-inert', '1');
          }}
        }}
      }}
      function decoratePickers(root) {{
        /* Make hidden, non-chat Type rows inert too.  This is scoped to rows
           BYO itself marked, so navigation back to a chat restores them. */
        var chatPicker = isChatSurface();
        markPlainTaskTypeRows();
        var allTypeNodes = document.querySelectorAll('[data-model-id^="' + PREFIX + '"]');
        for (var t = 0; t < allTypeNodes.length; t++) {{
          var typeRow = allTypeNodes[t].closest('button[role="option"], [role="option"], button');
          if (!typeRow) continue;
          if (!chatPicker && typeRow.getAttribute('data-kiro-byo-type-blocked') !== '1') {{
            if (!typeRow.hasAttribute('inert')) {{
              typeRow.setAttribute('inert', '');
              setAttr(typeRow, 'data-kiro-byo-type-blocked', '1');
            }}
          }} else if (chatPicker && typeRow.getAttribute('data-kiro-byo-type-blocked') === '1') {{
            typeRow.removeAttribute('inert');
            typeRow.removeAttribute('data-kiro-byo-type-blocked');
          }}
        }}
        if (!chatPicker) return;
        var scope = root && root.querySelectorAll ? root : document;
        var ids = scope.querySelectorAll('[data-model-id^="' + PREFIX + '"]');
        for (var i = 0; i < ids.length; i++) {{
          var el = ids[i];
          var wire = el.getAttribute("data-model-id") || "";
          setAttr(el, "data-kiro-byo-type-label", meta(wire).type);
          var picker = el.closest('[role="listbox"], [role="dialog"]');
          if (picker) {{
            var nativeAutos = picker.querySelectorAll('[data-model-id="auto"], [data-model-id="auto:jev"]');
            for (var a = 0; a < nativeAutos.length; a++) {{
              var autoRow = nativeAutos[a].closest('button[role="option"], [role="option"], button');
              if (autoRow) setAttr(autoRow, "data-kiro-byo-native-auto", "1");
            }}
          }}
        }}
        var containers = document.querySelectorAll('div:has(> button [data-model-id^="' + PREFIX + '"])');
        for (var c = 0; c < containers.length; c++) {{
          var container = containers[c];
          var rows = container.querySelectorAll(':scope > button[role="option"]');
          var firstType = null;
          var firstModel = null;
          var sawType = false;
          for (var r = 0; r < rows.length; r++) {{
            var isType = !!rows[r].querySelector('[data-model-id^="' + PREFIX + '"]');
            if (isType) {{
              if (!firstType) firstType = rows[r];
              sawType = true;
            }} else if (sawType && !firstModel && !rows[r].querySelector('[data-model-id="auto"], [data-model-id="auto:jev"]')) {{
              firstModel = rows[r];
            }}
          }}
          var oldType = container.querySelector(':scope > button[role="option"][data-kiro-byo-type-first="1"]');
          var oldModel = container.querySelector(':scope > button[role="option"][data-kiro-byo-model-first="1"]');
          if (oldType !== firstType) {{
            if (oldType) {{
              oldType.removeAttribute("data-kiro-byo-type-first");
              oldType.removeAttribute("data-kiro-byo-group-label");
            }}
            if (firstType) setAttr(firstType, "data-kiro-byo-type-first", "1");
          }}
          if (firstType) {{
            var firstWireNode = firstType.querySelector('[data-model-id^="' + PREFIX + '"]');
            var firstWire = firstWireNode ? (firstWireNode.getAttribute("data-model-id") || "") : "";
            setAttr(firstType, "data-kiro-byo-group-label", meta(firstWire).group || "形态 · 可手动切换");
          }}
          if (oldModel !== firstModel) {{
            if (oldModel) oldModel.removeAttribute("data-kiro-byo-model-first");
            if (firstModel) setAttr(firstModel, "data-kiro-byo-model-first", "1");
          }}
          syncTypeEffortBoundary(container);
        }}
      }}
      function walk(root) {{
        if (!root) return;
        if (root.nodeType === 3) {{ fixText(root); return; }}
        if (root.nodeType !== 1 && root.nodeType !== 9) return;
        var it = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        var n;
        while ((n = it.nextNode())) fixText(n);
      }}
      function touchesTypePicker(node) {{
        var el = node && node.nodeType === 3 ? node.parentElement : node;
        if (!el || !el.querySelector) return false;
        if (el.matches && el.matches('[data-model-id^="' + PREFIX + '"], [data-model-id="auto"], [data-model-id="auto:jev"]')) return true;
        if (el.querySelector('[data-model-id^="' + PREFIX + '"]')) return true;
        var picker = el.closest && el.closest('[role="listbox"], [role="dialog"]');
        return !!(picker && picker.querySelector('[data-model-id^="' + PREFIX + '"]'));
      }}
      function touchesLateTaskOptions(node) {{
        /* The scheduling editor often mounts first; its portal/listbox is
           inserted only after the user opens Model. These late plain-text
           options have no data-model-id, so touchesTypePicker misses them. */
        if (!document.documentElement.classList.contains(TASK_EDITOR_CLASS)) return false;
        var el = node && node.nodeType === 3 ? node.parentElement : node;
        if (!el || el.nodeType !== 1) return false;
        if (el.matches('[role="listbox"],[role="option"],[role="menuitem"]')) return true;
        if (el.querySelector('[role="listbox"],[role="option"],[role="menuitem"]')) return true;
        /* Guard menu implementations that use ordinary buttons. Only static
           Type labels qualify; never classify message bodies as options. */
        var candidates = el.matches('button') ? [el] : el.querySelectorAll('button');
        for (var j = 0; j < candidates.length; j++) {{
          var candidate = candidates[j];
          if (candidate.closest('.prose,.msg,pre,code,textarea,[contenteditable]')) continue;
          if (isKnownTypeLabel(String(candidate.textContent || '').trim())) return true;
        }}
        return false;
      }}
      function isKnownTypeLabel(value) {{
        for (var wire in LABELS) {{
          if (Object.prototype.hasOwnProperty.call(LABELS, wire) &&
              String(LABELS[wire].type || '').trim() === value) return true;
        }}
        return false;
      }}
      function scheduleDecorate(root) {{
        if (_decorateFrame) return;
        var run = function () {{ _decorateFrame = 0; decoratePickers(root || document); }};
        _decorateFrame = window.requestAnimationFrame ? window.requestAnimationFrame(run) : window.setTimeout(run, 0);
      }}
      function refresh(root) {{
        syncTaskEditorSurface();
        walk(root || document.body);
        decoratePickers(root || document);
      }}
      function start() {{
        refresh(document.body);
        new MutationObserver(function (records) {{
          var needsDecorate = false;
          var dialogsMayHaveChanged = false;
          for (var i = 0; i < records.length; i++) {{
            if (records[i].type === "childList") dialogsMayHaveChanged = true;
            var record = records[i];
            if (record.type === "characterData") {{
              fixText(record.target);
              if (touchesTypePicker(record.target)) needsDecorate = true;
            }}
            if (record.type === "attributes" && record.attributeName === "aria-selected" && touchesTypePicker(record.target)) {{
              needsDecorate = true;
            }}
            for (var j = 0; j < record.addedNodes.length; j++) {{
              var added = record.addedNodes[j];
              walk(added);
              if (!needsDecorate && (touchesTypePicker(added) || touchesLateTaskOptions(added))) needsDecorate = true;
            }}
          }}
          if (dialogsMayHaveChanged && syncTaskEditorSurface()) {{
            /* Closing a task editor restores the real chat Type picker. */
            walk(document.body);
            needsDecorate = true;
          }}
          if (needsDecorate) scheduleDecorate(document);
        }}).observe(document.body, {{
          subtree: true, childList: true, characterData: true, attributes: true,
          attributeFilter: ["aria-selected"]
        }});
      }}
      if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
    }})();
  </script>
'''


def guard_block(labels: dict[str, dict[str, str]] | None = None) -> str:
    """The complete BYO-owned chat-only Type picker presentation block."""
    if labels is None:
        labels = type_label_map()
    return _GUARD_STYLE + _label_script(labels) + f"  {GUARD_END}\n"




def _bundle_has_model_id_contract(dist: Path) -> bool:
    """Prove the installed dashboard still exposes ModelDropdownList ids.

    ``data-model-id`` is authored source/DOM contract and survives minification;
    unlike helper-function syntax, its spelling is not affected by variable
    mangling or object-spread lowering.  Multiple chunks may contain the literal
    (source maps/preloads/code splitting), so existence rather than uniqueness is
    the correct invariant.
    """
    for path in sorted(dist.rglob("*.js")):
        if not path.is_file():
            continue
        try:
            # Avoid loading multi-megabyte chunks twice in the common case.
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    if "data-model-id" in chunk:
                        return True
        except OSError:
            continue
    return False


def _inject_guard(index_text: str, labels: dict[str, dict[str, str]] | None = None) -> tuple[str, bool]:
    wanted = guard_block(labels)
    if HTML_GUARD_MARKER in index_text:
        # Upgrade an older guard in place so style changes reach existing
        # installs; the block is Kiro BYO-owned and delimited by its id (and,
        # since the label script, by an explicit end marker).
        start = r"\n?[ \t]*<style id=\"" + re.escape(HTML_GUARD_ID) + r"\">"
        block = re.compile(start + r".*?" + re.escape(GUARD_END) + r"\n?", flags=re.DOTALL)
        found = list(block.finditer(index_text))
        if not found:
            legacy = re.compile(start + r".*?</style>\n?", flags=re.DOTALL)
            found = list(legacy.finditer(index_text))
        if len(found) != 1:
            raise DashboardPatchError(
                f"expected one Kiro BYO picker guard block, found {len(found)}"
            )
        current = found[0]
        if current.group(0) == wanted:
            return index_text, False
        upgraded = index_text[: current.start()] + wanted + index_text[current.end():]
        return upgraded, True

    # index.html is a build artifact but its shell structure is deliberately
    # stable. Requiring a single </head> prevents us from guessing on malformed
    # or radically changed dashboard packaging.
    matches = list(re.finditer(r"</head\s*>", index_text, flags=re.IGNORECASE))
    if len(matches) != 1:
        raise DashboardPatchError(
            f"expected one dashboard </head> insertion point, found {len(matches)}"
        )
    match = matches[0]
    patched = index_text[: match.start()] + wanted + index_text[match.start() :]
    return patched, True


def _stabilize_guard(
    index_text: str,
    *,
    labels: dict[str, dict[str, str]] | None = None,
    max_passes: int = 4,
) -> tuple[str, bool]:
    """Return an inspector-stable dashboard guard rewrite.

    ``_inject_guard`` is intended to be idempotent, but the integration manager
    must not assume that one historical guard generation always upgrades to the
    current generation in one textual step.  Converge in memory and prove that
    applying the same current guard again is a no-op before any dashboard bytes
    are committed.
    """
    if labels is None:
        labels = type_label_map()
    original = index_text
    current = index_text
    for pass_index in range(max_passes + 1):
        patched, changed = _inject_guard(current, labels)
        if not changed:
            return current, current != original
        if pass_index >= max_passes:
            break
        if patched == current:
            raise DashboardPatchError(
                "dashboard guard rewrite made no progress while still reporting changed"
            )
        current = patched
    raise DashboardPatchError(
        f"dashboard guard did not converge after {max_passes} passes"
    )


def discover_dashboard_patches(crew_package: Path) -> list[DashboardAssetPatch]:
    """Return the stable dashboard rewrite needed for BYO picker semantics.

    KiroCrew ships ``website/dist`` as ``kiro_crew/static/dist``.  We patch only
    that shell's ``index.html`` after proving the compiled picker still carries
    its ``data-model-id`` DOM contract.  This avoids all dependency on
    content-hashed asset names or minified helper shapes.
    """
    dist = crew_package / "static" / "dist"
    if not dist.is_dir():
        raise DashboardPatchError("KiroCrew dashboard dist directory is missing")

    index = dist / "index.html"
    if not index.is_file():
        raise DashboardPatchError("KiroCrew dashboard index.html is missing")

    if not _bundle_has_model_id_contract(dist):
        raise DashboardPatchError(
            "KiroCrew dashboard model-row contract changed: data-model-id was not found"
        )

    try:
        original = index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DashboardPatchError(f"cannot read KiroCrew dashboard index.html: {exc}") from exc

    patched, changed = _stabilize_guard(original, labels=type_label_map())
    return [DashboardAssetPatch(index, original, patched, changed)]
