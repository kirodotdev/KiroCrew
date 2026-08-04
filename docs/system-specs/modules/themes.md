# Themes Module

Last Updated: 2026-07-23

## Overview

The dashboard is fully skinnable. A **theme** ranges from a bare color palette
up to a full "experience pack" (fonts, sandboxed overlays, audio, persona). A
color theme is the degenerate case of a pack — the whole spectrum lives behind
**one Theme dropdown** in Settings → Display: install many, select one.

Themes are a **standalone subsystem built on `useTheme`**, not KiroCrew apps. This document is the **source of truth** for the end-to-end subsystem.
The frontend pack-author contract (the CSS-var surface and the
`overrides.css` selector allowlist) lives in
[`website/docs/theming-contract.md`](../../../website/docs/theming-contract.md);
where the two overlap, this spec governs.

## Capability Tiers

`theme.json` MUST declare `"formatVersion": 1` (required integer major,
mirroring the platform layer's pinned-`CONTRACT_VERSION` precedent). Validation
rejects a missing/non-integer value, and rejects an unknown major with an
explicit *"this pack requires a newer version of KiroCrew"* message — never the
opaque generic-validation errors — so an older KiroCrew degrades honestly when
handed a newer pack. Semantics changes within a major stay backward-tolerant;
breaking manifest changes bump the major.

`theme.json` also declares a `level` (0/1/2) that gates which asset categories
a pack may ship. Validation is tier-scaled to payload trust.

| Tier | `level` | Surface unlocked |
|---|---|---|
| **L0 Color** | 0 | the 43 theme CSS variables (dark + light) only |
| **L1 Branded** | 1 | + `branding/` (logo, favicon, wordmark), `styles/fonts/`, scoped `overrides.css` |
| **L2 Experience** | 2 | + `overlays/` + `topbar/` sandboxed HTML, `audio/`, `persona.md` |

Constants (`dashboard/theme_validate.py`): `_THEME_MAX_LEVEL=2`,
`_THEME_MAX_FONTS=3`, `_THEME_MAX_OVERLAYS=5`, `_THEME_PERSONA_MAX_CHARS=2000`,
plus per-file byte caps (`_THEME_FILE_CAPS`) and per-level entry-count + total
uncompressed byte ceilings.

## Install Pipeline

1. **Source** — a local directory (moved/copied) or an https `github.com` repo
   shallow-cloned server-side (`_clone_github`, `--depth 1`, 30s timeout, host
   allowlist).
2. **Stage** — the source is copied into a private staging snapshot
   (`.install-staging-<token>`) via a per-file, symlink-rejecting,
   byte-bounded loop (`_copy_installed_theme`). The source dir remains
   attacker-writable throughout, so nothing read from it is trusted twice:
   the copy enforces a hard cumulative byte ceiling, and everything after
   this step operates on the snapshot only.
3. **Validate** — `_validate_theme_dir(stage)` runs on the immutable staging
   snapshot and returns `(record | None, error)`: tier-gated category
   allowlist, filename allowlist, per-file/total size caps, symlink rejection,
   path-traversal rejection (`_safe_slug`), CSS/HTML denylists, audio
   magic-byte sniff, and persona bounds. Validating the snapshot (not the
   source) closes the validate/copy TOCTOU class.
4. **Promote** — the validated snapshot is atomically renamed into
   `~/.kiro/crew/themes/<slug>/`. Concurrent installs of the same slug are
   serialized so staging never clobbers a live pack. **Re-install overwrites**
   (the update path); a `409` is returned only when a slug collides with an
   editor-created custom record.

## HTTP Routes

Registered in `dashboard/server.py`. The validation/parsing core lives in
`dashboard/theme_validate.py` (constants, CSS tokenizer, `_validate_*`,
`_theme_asset_descriptor`, path/slug helpers); the HTTP handlers below live in
`dashboard/handlers/themes.py`:

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/api/themes/install` | Install from local dir or GitHub (overwrite on re-install) |
| `DELETE` | `/api/themes/{slug}` | Remove an installed theme |
| `GET` | `/api/themes` | List all themes (built-in + custom + installed) |
| `GET` | `/api/themes/{slug}` | Theme detail + resolved `level` |
| `GET` | `/api/theme/{slug}/assets/{path}` | Serve a pack asset (nosniff + content-type allowlist) |
| `GET` | `/api/theme/{slug}/overlay/{id}` | Serve overlay HTML (locked CSP) |
| `GET` | `/api/theme/{slug}/topbar/{mode}` | Serve topbar HTML for `dark`/`light` (locked CSP) |

(`GET /api/theme/boot` and the editor CRUD `POST/PUT /api/themes[/{slug}]`
predate this subsystem and remain the color-theme surface.)

## Security Model

- **Sandboxed rendering** — overlays and topbars render in
  `sandbox="allow-scripts"` iframes **without** `allow-same-origin`, so they get
  no cookies, storage, or same-origin fetch. This is the real runtime boundary;
  install-time denylists are defense-in-depth.
- **Locked CSP** — overlay/topbar responses carry a fixed
  `Content-Security-Policy` including a `sandbox` directive; asset responses
  carry `X-Content-Type-Options: nosniff` and a content-type allowlist.
- **postMessage allowlist** — the parent (`ThemeExperienceLayer.tsx`) accepts
  only `theme:resize`, `theme:sound`, `theme:visibility`, and `theme:state`
  messages from a pack iframe; all others are dropped.
- **CSS containment** — install-time denylist (no `@import`, external `url()`,
  dangerous functions/bindings, forbidden selectors, `z-index` >
  `_THEME_OVERLAY_MAX_ZINDEX`) via a string-aware top-level rule tokenizer, plus
  a **runtime positive-selector scoper** (`_scopeOverridesCss`) that keeps only
  allowlisted selector forms and drops every non-`@media` at-rule.
  The scoper is the **load-bearing runtime boundary** for attacker-authored
  `overrides.css` injected into the main document; both hand-rolled parsers are
  slated for consolidation onto CSSOM rule-walking as a **pre-1.0 blocker**,
  tracked in kirodotdev/KiroCrew#316 (milestone-bound to the first L1-pack
  release). Until then, drift between the two parsers — and between the scoper
  and real browser CSS parsing (escapes, comments, exotic at-rules) — is pinned
  by the shared adversarial corpus `test/fixtures/theme_css_corpus.json`,
  asserted against **both** parsers (pytest + vitest); new evasion classes MUST
  land as corpus cases.
  Same-pack-relative `url()`s are rewritten to absolute asset-route URLs
  (`_rewriteOverridesUrls`); fonts are declared via `theme.json` `fonts` (served
  as `font/ttf`) and injected by `injectThemeFonts`, since an `@font-face` in
  `overrides.css` would be scoped away.
- **HTML containment** — overlay/topbar HTML is rejected at install if it
  references external scripts, `fetch`/`XMLHttpRequest`, `document.cookie`, or
  `localStorage`/`sessionStorage`.
- **Persona** — `persona.md` capped at 2000 chars (`_THEME_PERSONA_MAX_CHARS`,
  the enforced control). `_validate_persona` additionally runs an **advisory
  keyword check** for boundary phrases (e.g. a "drop persona" / security-override
  clause): this is a hygiene nudge on the pack author, **not** a security
  control — it is a substring scan that carries no runtime authority and is
  trivially satisfiable. The enforced server-side controls are (1) the hard
  2000-char length bound and (2) **content-binding** (the injected text must
  hash-match the value the request supplies). The consent flow itself is a
  **client-side UX affordance**, not server-enforced authorization — see below.

  **Consent gate (what it actually guarantees — and what it does not).** For an
  INSTALLED pack, the consent modal shows the persona text **verbatim** and the
  user's grant is keyed to `sha256(persona_text)`, stored in the browser's
  localStorage. On every first-turn chat the frontend threads that hash as
  `theme_consent_sha`; `_maybe_inject_persona` (`chat_utils.py`)
  injects the pack's persona **only when `theme_consent_sha` equals sha256 of
  the persona text it reads from disk at that moment** (constant-time compare).
  A missing hash, or a stale one after a reinstall rewrote `persona.md`, **fails
  closed** — the new, never-consented text is not injected and the user is
  re-prompted. This is **surprise-prevention**: *what you saw and consented to
  is exactly what can be injected*. It is **not** server-enforced consent: the
  server never records a grant, `GET /api/themes/{slug}` returns
  `personaInfo.sha256` (and the text) to any authenticated caller, so an
  authenticated API client can synthesize a valid `theme_consent_sha` without a
  human ever seeing the modal. Consent also lives in one browser's
  localStorage, so headless/multi-browser clients have no consent story. Anyone
  extending persona scope (longer bound, per-turn injection, richer tiers) MUST
  NOT build on a server-side-consent invariant — it does not exist. The legacy
  boolean `theme_consent` request field is still parsed for backward-compatible
  bodies/logging but **grants no injection on its own**.

  **All personas come from installed packs** and are content-bound
  consent-gated by the mechanism above — there is **no built-in / unconditional
  persona path**. A theme injects a persona only via a validated `persona.md`
  in an installed pack (`custom-<slug>`), and only when the caller's
  `theme_consent_sha` matches the on-disk text. There is no trusted first-party
  registry that injects without consent.

  **Scope of a persona.** A persona is **tone/voice only**. It cannot change the
  agent's available tools, its refusals, or any guardrail — it is appended as
  ordinary text appended to the **first user message** of a session (not the
  system prompt) with no runtime authority over policy.

  **Governance.** Persona injection is gated by the
  `capabilities.theme_persona` governance capability (`SCOPE_CATALOG`,
  `capability_default=True`): standalone it defaults to allow, but an
  enterprise POLICY can force-disable installed-pack persona injection
  wholesale (consulted at the `chat_runner.py` injection site; a denying policy
  skips injection silently). See `governance.md`.

## Frontend Integration Points

| Surface | File | Role |
|---|---|---|
| Loader | `website/src/hooks/useTheme.tsx` | Applies CSS vars; `applyThemeOverrides` → `_scopeOverridesCss` + `_rewriteOverridesUrls`; `injectThemeFonts`; pre-apply self-repair; `themeSwitching` state |
| Experience layer | `website/src/components/ThemeExperienceLayer.tsx` | Mounts sandboxed overlay/topbar iframes + audio; enforces the postMessage allowlist |
| Settings UI | `website/src/pages/settings/DisplayPanel.tsx` | Single Theme dropdown + install-from-local/GitHub + remove + "Applying…" status indicator |

## Sample / Test Packs

No theme-bearing packs ship in the code package (wheel/sdist/frozen). Sample
and test packs live **outside the repo**; the full-L2 install/validate/persona
regression is exercised entirely from fixtures built in a temp directory (see
`test/test_theme_install.py::TestFullL2Fixture`), so nothing shippable carries
a persona or third-party-derived branding.

This covers **art assets too**, not just personas and manifests. The
built-in-theme removal deleted the persona markdown but left nine unreferenced
image/font/video files behind in `src/kiro_crew/static/`, which
`MANIFEST.in`'s `recursive-include src/kiro_crew/static *` kept shipping in the
sdist, wheel and DMG. They are gone. When retiring a theme, delete its art in
the same change: every file under `src/kiro_crew/static/` ships, so an orphan
there is a shipped orphan, not dead weight in a dev tree.
