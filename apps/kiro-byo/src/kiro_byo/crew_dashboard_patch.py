"""Stable dashboard guard for KiroCrew's model picker.

KiroCrew 0.9 synthesizes its native ``auto`` and optional ``auto:jev`` rows in
React after ``GET /api/models`` returns.  Kiro BYO therefore cannot suppress
those rows from the Python endpoint alone.

Older Kiro BYO builds rewrote minified, content-hashed JavaScript helpers.  That
was needlessly coupled to bundler output and broke on KiroCrew 0.9.0-insider.6
when the same source compiled to a different expression shape.

The shipped picker has a much more stable public DOM contract instead:
``ModelDropdownList`` renders every option as a button containing
``data-model-id=<wire model id>``.  We inject one tiny stylesheet into the
shipped ``static/dist/index.html`` and hide Crew's synthetic Auto/Jev rows only
when the *same list* contains a Kiro BYO virtual Type id.  Consequently:

* Kiro BYO backend -> the active Mode's declared Types + concrete BYO models,
  with no native Auto/Jev rows;
* official/other backends -> no ``kiro-byo-type:*`` row exists, so Crew's
  original Auto/Jev UI is untouched.

Discovery remains fail-closed: before modifying HTML we prove that the installed
bundle still contains the ``data-model-id`` picker contract.  We no longer try
to reverse-engineer minified function bodies.
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
_GUARD_STYLE = f'''\n  <style id="{HTML_GUARD_ID}">\n    /* {HTML_GUARD_MARKER} */\n    div:has(> button [data-model-id^="{TYPE_PREFIX}"]) > button:has([data-model-id="auto"]),\n    div:has(> button [data-model-id^="{TYPE_PREFIX}"]) > button:has([data-model-id="auto:jev"]) {{\n      display: none !important;\n    }}\n    /* Mode-owned Types are dynamic. The injected script annotates each Type\n       row from its namespaced wire id, so this CSS never needs to know whether\n       the active Mode is Tiga, Dyna, Gaia, or a user-supplied pack. */\n    [data-model-id^="{TYPE_PREFIX}"] {{\n      font-size: 0 !important;\n    }}\n    [data-model-id^="{TYPE_PREFIX}"]::after {{\n      content: attr(data-kiro-byo-type-label);\n      font-size: 13px;\n    }}\n    /* Keep group captions outside the option's painted box. Crew paints the
       selected/hover background on the button itself, so captions rendered in
       the button content get highlighted together with the first row. Put the
       captions in the button's margin instead. */
    button[role="option"][data-kiro-byo-type-first="1"],
    button[role="option"][data-kiro-byo-model-first="1"] {{
      position: relative;
    }}
    button[role="option"][data-kiro-byo-type-first="1"] {{
      margin-top: 24px;
    }}
    button[role="option"][data-kiro-byo-model-first="1"] {{
      margin-top: 34px;
    }}
    button[role="option"][data-kiro-byo-type-first="1"]::before,
    button[role="option"][data-kiro-byo-model-first="1"]::before {{
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
    button[role="option"][data-kiro-byo-type-first="1"]::before {{
      content: "形态 · 每轮按任务选模型";
      top: -20px;
    }}
    button[role="option"][data-kiro-byo-model-first="1"]::before {{
      content: "具体模型 · 固定使用";
      top: -30px;
      border-top: 1px dashed var(--border-strong, var(--border));
      padding-top: 8px;
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
        mode = (pack.short_name or pack.display_name or pack.id).strip()
        for policy in pack.policies:
            type_name, picker_label = _type_names(policy.display_name, policy.id)
            wire = type_selection_id(pack.id, policy.id)
            labels[wire] = {
                "full": f"{mode}{LABEL_SEPARATOR}{type_name}",
                "type": picker_label,
            }
    return dict(sorted(labels.items()))


def _label_script(labels: dict[str, dict[str, str]]) -> str:
    import json

    table = json.dumps(labels, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    return f'''  <script id="kiro-byo-model-label">
    (function () {{
      var PREFIX = "{TYPE_PREFIX}";
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
        return {{ full: mode + "{LABEL_SEPARATOR}" + type, type: type + " Type" }};
      }}
      function isPinAction(node) {{
        var el = node.parentElement;
        if (!el) return false;
        var row = el.closest('button, [role="menuitem"], [role="option"], li') || el;
        var text = String(row.textContent || "");
        // Crew localizes the pin row. Match the action idea rather than one
        // exact sentence; the composer/status chip carries none of these words.
        return /置顶|固定|pin(?:ned)?|ピン/i.test(text) ||
          (/\bdefault\b/i.test(text) && /代理|agent/i.test(text));
      }}
      function replacementFor(node, wire) {{
        var value = meta(wire);
        return isPinAction(node) ? value.type : value.full;
      }}
      function skip(node) {{
        var el = node.parentElement;
        // Picker rows are relabelled through data attributes below; transcripts
        // and code intentionally keep raw text untouched.
        return !el || !!el.closest("[data-model-id], .msg, .prose, pre, code, textarea, [contenteditable]");
      }}
      function fixText(node) {{
        var v = node.nodeValue;
        if (!v || v.indexOf(PREFIX) < 0 || skip(node)) return;
        var nv = v.replace(RE, function (wire) {{ return replacementFor(node, wire); }});
        if (nv !== v) node.nodeValue = nv;
      }}
      function decoratePickers(root) {{
        var scope = root && root.querySelectorAll ? root : document;
        var ids = scope.querySelectorAll('[data-model-id^="' + PREFIX + '"]');
        for (var i = 0; i < ids.length; i++) {{
          var el = ids[i];
          var wire = el.getAttribute("data-model-id") || "";
          el.setAttribute("data-kiro-byo-type-label", meta(wire).type);
        }}
        var buttons = document.querySelectorAll('button[role="option"]');
        for (var j = 0; j < buttons.length; j++) {{
          buttons[j].removeAttribute("data-kiro-byo-type-first");
          buttons[j].removeAttribute("data-kiro-byo-model-first");
        }}
        var containers = document.querySelectorAll('div:has(> button [data-model-id^="' + PREFIX + '"])');
        for (var c = 0; c < containers.length; c++) {{
          var rows = containers[c].querySelectorAll(':scope > button[role="option"]');
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
          if (firstType) firstType.setAttribute("data-kiro-byo-type-first", "1");
          if (firstModel) firstModel.setAttribute("data-kiro-byo-model-first", "1");
        }}
      }}
      function walk(root) {{
        if (root.nodeType === 3) {{ fixText(root); return; }}
        if (root.nodeType !== 1 && root.nodeType !== 9) return;
        var it = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        var n;
        while ((n = it.nextNode())) fixText(n);
      }}
      function refresh(root) {{
        decoratePickers(root);
        walk(root || document.body);
      }}
      function start() {{
        refresh(document.body);
        new MutationObserver(function (records) {{
          var needsDecorate = false;
          for (var i = 0; i < records.length; i++) {{
            var record = records[i];
            if (record.type === "characterData") fixText(record.target);
            for (var j = 0; j < record.addedNodes.length; j++) walk(record.addedNodes[j]);
            if (record.addedNodes.length) needsDecorate = true;
          }}
          if (needsDecorate) decoratePickers(document);
        }}).observe(document.body, {{ subtree: true, childList: true, characterData: true }});
      }}
      if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
    }})();
  </script>
'''


def guard_block(labels: dict[str, dict[str, str]] | None = None) -> str:
    """The complete Kiro BYO-owned <head> block: picker CSS + label script."""
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

    patched, changed = _inject_guard(original)
    return [DashboardAssetPatch(index, original, patched, changed)]
