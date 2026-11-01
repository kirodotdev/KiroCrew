# Kiro BYO 0.2.2 — i18n / visible reasoning implementation report

**Scope:** Latest user-supplied `Kiro_BYO_0.2.2-routing-speed-fix-source.zip` used
as the sole implementation baseline. No files from earlier generated versions
were substituted. Product SemVer remains **0.2.2** on every version-bearing
surface. No KiroCrew minified JS bundle was edited.

## 1. Audit of pre-existing language path

Prior to this patch the product already offered a visible reasoning language
preference (`provider_admin.reasoning_language_settings` -> config preferences ->
`AcpServer._apply_reasoning_language_policy` -> `AcpServer._runtime_system_message`
-> provider adapter -> authentic ACP streaming). A local script-aware language
heuristic already stripped fenced and inline code. Its best-effort instruction
could influence some models' exposed reasoning and summaries, but it could not
alter provider-private hidden thinking, and some upstream providers ignored it.
The main KiroCrew App UI was almost entirely Chinese-coded and had **no
separate persisted UI language or final-response language choice**. ACP event
payload types were not a suitable place for post-hoc translations.

## 2. Changes implemented

### UI language

- Five translation catalogues are shipped: `zh-CN`, `zh-TW`, `en`, `ja`, `ko`.
- **511 keys per locale**, including static and parameterized product copy,
  common alerts/states, mode descriptions, language settings, errors and chart
  labels. The locale file key sets and parameter signatures are identical.
- `i18n.py` provides safe normalization, cached catalogue loading and a small
  additive management-API error classifier. The actual KiroCrew App backend
  supports `GET /locales/{locale}` and `PUT /preferences/ui-language`.
- The React App switches catalogues in-place on preference changes, re-renders
  without any KiroCrew reload, session restart, integration reinstall or
  global fetch wrapper. Preferences survive restart via the existing locked
  `preferences` file data path. If BYO explicitly chooses a locale it wins;
  otherwise the documented host DOM language, then browser/WebView locale,
  then English are used. There is **no invented KiroCrew language API**.
- Model IDs, Provider IDs, Type selection IDs, protocol keys, tool JSON and
  technical logs remain unchanged; Type labels, health messages and built-in
  Tiga descriptions are localized at the presentation boundary.
- `ui/index.mjs`: all originally Chinese React string constants/templates moved
  to keyed lookups. Collision between a pre-existing `const t` variable and
  the translation helper was resolved by naming the helper `tr`.

### Final answer / visible reasoning

- Response preference (`auto`, `zh-CN`, `zh-TW`, `en`, `ja`, `ko`) is independent
  of UI locale and independently saved via `PUT /preferences/response-language`.
- Auto uses the current user's prose, ignores fenced code / inline identifiers,
  supports explicit language requests, and falls back to recent user messages
  for short or ambiguous input; then English. It requires no extra paid model.
- A clear explicit request in the current turn outranks the response default.
- Existing visible reasoning preferences are preserved and applied through a
  single instruction per turn; final answer and reasoning instructions are
  combined in ephemeral session policy, passed through the main provider,
  worker, relay and reviewer paths without altering reasoning effort or
  thinking toggles. Subsequent user turns re-evaluate the policy.
- **Provider compliance remains best-effort.** A provider can ignore the
  visible-reasoning preference. The system does not translate, infer or expose
  private internal chain-of-thought.
- Optional post-hoc translation of already visible provider summaries is
  intentionally **not implemented**: the existing ACP/host presentation
  channel lacks a validated second payload label for “translated excerpt”
  and original/translated side-by-side. Adding a synthetic reasoning event
  would incorrectly blur their provenance. Original streamed content is intact.

### Chart

- `ui/index.mjs`: Overview “runtime performance trends” now plots a **24-hour
  response-latency line chart** from the existing telemetry's `hourly.avg_ms`,
  not request-count bars. Hours lacking a valid latency sample break the line;
  no fabricated data points are generated. Tooltips expose actual time,
  duration and request count.

### Error codes

- App and standalone management HTTP routes can include a stable `code` for
  recognizable errors (`SESSION_BUSY`, `INTEGRATION_INCOMPATIBLE`,
  `PROVIDER_AUTH_FAILED`, `MODEL_NOT_AVAILABLE`). Original error text remains
  for diagnosis. This does **not** change ACP JSON-RPC error codes or streams.
  Unrecognized technical errors intentionally remain raw.

## 3. Modified source files compared with sole baseline

| File | Purpose |
|---|---|
| `backend/routes.py` | Actual KiroCrew App locale/preference endpoints, status, additive errors |
| `ui/index.mjs` | React catalogue lookups, selector, locale switch, mode/health copy, SVG chart |
| `src/kiro_byo/i18n.py` **new** | Locale resolver, validation, resources and stable management error taxonomy |
| `src/kiro_byo/locales/{en,ja,ko,zh-CN,zh-TW}.json` **new** | Five equivalent translated dictionaries |
| `src/kiro_byo/reasoning_language.py` | Explicit-response instructions, independent automatic final-answer policy |
| `src/kiro_byo/config.py` | Configuration field for independent final response language |
| `src/kiro_byo/provider_admin.py` | Saved UI/response preferences; existing reasoning preference retained |
| `src/kiro_byo/acp_server.py` | Ephemeral session values for combined language instruction |
| `src/kiro_byo/turn_runner.py` | Per-turn injection and worker/reviewer propagation |
| `src/kiro_byo/web_ui.py` | Local management API locale/preference access and status (legacy HTML not fully localized) |
| `pyproject.toml` | Wheel package-data includes locale JSON files |
| `scripts/release_check.py` | Language-aware checks that no longer require Chinese UI literals |
| `tests/test_i18n.py` **new** | Locale parity, detection, isolation, persisted settings, chart/route guards |
| `tests/test_app_package.py` | Existing UI assertions adapted to verify actual locale assets and lookups |
| `tests/test_management_ui.py` | View-mode and colored-status assertions preserve i18n semantics |
| `tests/test_thinking_preferences.py` | Thinking UI contract asserts localized lookup keys |
| `tests/test_acp_lifecycle_failures.py` | Wait for observed writer-thread termination after failed transport, removing race |
| `I18N_TERMINOLOGY.md` **new** | Multilingual product terminology glossary |
| `I18N_IMPLEMENTATION_REPORT.md` **new** | This independently auditable report |

No files were removed. Provider catalogues, Type Policy Pack, dynamic routing,
Jev seam, durable checkpoint storage, session ownership and KiroCrew patches
were not restructured.

## 4. Tests and results

- **pytest:** `678 passed, 1 skipped, 42 subtests passed` on Linux/Python 3.13.
  The skip is the host-dependent integration test
  `tests/test_activation_truth.py:134` requiring `KIRO_BYO_E2E_CREW=1`
  and KiroCrew's bundled Python after Install/Update.
- New `tests/test_i18n.py`: **17 passing cases**, including six parameterized
  normalizer cases; tested mixed-language/code input, a conversation change,
  explicit user preference, preference independence, JSON duplicate detection,
  fallback and missing/unknown reasons, update paths and markup checks.
- Python `compileall` for source + backend: passed.
- `node --check ui/index.mjs`: passed.
- `scripts/release_check.py`: version/documentation/route contracts passed.
- Chromium browser smoke test (script executed with system Chromium and
  Playwright, stubbing React component construction) verified all five UI
  catalogue lookups, interpolation, actual SVG latency rendering and host
  language resolution. This is **not** an integrated KiroCrew UI E2E test.
- Distribution check: source/release runtime digests equal. All five locale
  files are included in the App Release, the Source ZIP and the built wheel.
- No real Windows/macOS platform run, real KiroCrew WebView interaction,
  real Provider language adherence or real ACP end-to-end stream was
  performed; they must not be inferred from static or unit tests.

## 5. Known limitations / strict-acceptance deviations

1. **Legacy standalone CLI management HTML** (`web_ui.py` `_page`) still has
   Chinese hard-coded labels and messages. Its newly supported preference
   endpoints work, but full five-language DOM text localization is **not
   complete on that secondary page**. The primary KiroCrew App React UI does
   use the five-language catalogue. Consequently the literal acceptance
   criterion “all BYO surfaces fully localized” remains **partially unmet**.
2. Some provider-/Mode Pack-supplied free-form descriptions and raw diagnostic
   fields are displayed verbatim. This is intentional for trace fidelity;
   future Mode Packs such as Dyna need their own translation resources.
3. Provider-visible reasoning is best-effort and cannot guarantee the exposed
   text is in the requested language; provider-native summaries and hidden
   reasoning are not modified. The optional visible-summary translation is
   withheld pending a safe separate original/translation display channel.
4. Locale auto-follow uses documented host `lang` when present, otherwise
   WebView language. A dedicated, reliable KiroCrew locale API was not found
   in the uploaded source and was not invented.

## 6. Build identity

- SemVer: **0.2.2**, unchanged.
- Build runtime fingerprint (SHA256, deterministic set of runtime files):
  `aff112d444014c65e609b67f849fbfb9f85eff7855c6fe120f9e81531481e74f`
- Distribution archive SHA-256 values are stored in adjacent
  `SHA256SUMS.txt`, produced by `scripts/build_release.py`.

## 7. Follow-up verification for the integrator

1. Install/update Kiro BYO App and integration using normal KiroCrew steps.
2. In Settings > language, switch all five UI locales. Verify immediate
   page-level changes with no KiroCrew reload or ACP session creation.
3. Test English UI + Chinese question, Chinese UI + English question and
   Japanese question + Korean UI. Inspect outgoing provider system context
   if authorized, without dumping secret keys or hidden reasoning.
4. Test visible reasoning with a model exposing summary and another model
   exposing no summary. Record the provider compliance as observed, not
   guaranteed.
5. Check Windows/macOS/Linux with host-managed KiroCrew and confirm no
   regression in ACP streaming and provider/model selection.
