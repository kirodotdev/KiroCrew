---
title: App registry sources in private GitHub repositories, read through the host's GitHub CLI sign-in
status: draft
author: buluoray
created: 2026-11-17
last-audited: 2026-11-17
audited-at: 0e618612ac5
doc-pr: null
implementation-prs: [38999]
tracking-issues: [38988]
supersedes: []
superseded-by: []
---

# RFC: App registry sources in private GitHub repositories

- Status: draft. Nothing is on main. Verified at `0e618612ac5`.
- Scope: the App Store's external registries and the apps they list, when the
  repository is a private `https://github.com/<owner>/<repo>` repository.

## Summary

A user who adds a registry that lives in a private GitHub repository gets
"sign-in refused" today, even when the GitHub CLI (`gh`) on the computer that
runs Kiro Crew is signed in with access to that repository. This RFC lets the
registry read such a repository through `gh`, without Kiro Crew ever seeing the
GitHub token. Anonymous access, and the user's own git credentials, are tried
first. `gh` is used only when that fails, only for repositories the owner
designated, and every check a public source gets still applies.

## Motivation

### How sources are fetched today

Every registry source is a git repository and every fetch is a `git` command.
Nothing downloads raw files, release assets or tarballs.

- The registry index is a shallow `git clone` of the configured repository
  (`_fetch_external_registry_index`, `apps/registry_pipeline/indexes.py`). It
  reads `app-registry.json`, or scans `apps/*/app.json` when there is none.
- An app's card details come from a shallow clone of the app's repository
  (`_fetch_app_manifest`, `apps/registry_pipeline/manifests.py`), cached under
  the manifest cache.
- Install and update clone, fetch or pull into the app source directory
  (`_git_clone_or_pull` and `_git_fetch_ref`,
  `apps/registry_pipeline/checkout.py`).
- Integrity: a pinned app is fetched by commit and the checkout must report that
  exact commit (`_git_fetch_ref`, "pinned commit verification failed"). An
  existing checkout is reused only when its `origin` is the same repository and
  branch. Admission (`apps/admission.py`) judges the cloned `app.json`. Hooks,
  fsmonitor, submodules and `ext::` are turned off on every git spawn.
- Credentials follow who chose the repository. Index-chosen repositories run
  with `anonymous_git_env()` and the strict sandbox, so an untrusted index
  cannot make Kiro Crew read a private sibling repository with the owner's
  identity. The configured index repository, apps in that same repository
  (`_is_owner_designated_repo`), and `owner`-tier registries use
  `minimal_env()`, which keeps the owner's git config and credential helpers.
- Failures reach the dashboard as a fixed cause (`dns`, `host_key`, `auth`,
  `network`, `timeout`, `other`, in `git_targets.py`), and
  `website/src/components/appstore/registryRefreshMessage.ts` turns each into a
  remedy sentence.

### The problem

An `https://` clone runs in the `strict` sandbox, which hides `~/.config/gh`.
A user who signed in with `gh auth login` and `gh auth setup-git` has a git
credential helper that calls `gh`, but inside the sandbox that `gh` cannot read
its own sign-in. The clone fails and the user sees "sign-in refused", with no
way to fix it from the dashboard. The users this matters for have no terminal,
so "configure a credential helper" is not an answer either.

## Goals

1. A registry index, its card details and its apps can come from a private
   GitHub repository the host's `gh` can read.
2. Kiro Crew never reads, copies, logs or stores the GitHub token. It never runs
   `gh auth token`. `gh` hands the credential to `git` itself.
3. Every integrity check that applies to a public source applies unchanged.
4. When `gh` is missing, signed out, or has no access, the dashboard says so
   and says what to do. Nothing falls back silently. Nobody is asked to paste a
   token.
5. Nothing new to configure. Adding a private registry works from the existing
   Add Registry form.

## Non-goals

- GitHub Enterprise Server hosts. Only `github.com` in this RFC.
- A "this source is private" setting. Trying anonymous first, then `gh`, needs
  no new config key, so no persisted format changes.
- Letting an untrusted index reach private sibling repositories. Index-chosen
  repositories stay anonymous; see Security.
- Downloading tarballs or release assets. See Alternatives.
- The `gh` retry in strict provider-binary mode (`KIROCREW_PROVIDER_BIN_STRICT`).
  That mode trusts only the binaries on the shared trusted list, and git is not
  on it. Adding git would change what strict mode trusts for every caller, so it
  needs its own review. Until then a private GitHub source fails closed in
  strict mode with cause `gh_strict_mode`, and the dashboard says the retry is
  not available in strict mode and how to reach the repository another way.

## Design

### When `gh` is used

`gh` is a second attempt, never the first. A fetch takes it only when all of
these hold:

1. The fetch already runs with the owner's credentials today: the configured
   index repository, an app in that same repository, or an install the
   `owner` tier confirms. An index-originated (anonymous) fetch never does.
2. The URL is exactly `https://github.com/<owner>/<repo>` (optionally `.git`),
   with no user info, port, query or fragment. `owner` matches
   `^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$`, `repo` matches `^[A-Za-z0-9._-]{1,100}$`
   and is not `.` or `..`. Anything else is refused before any process starts.
3. The first attempt failed the way a private repository fails: git's output
   reads as refused authentication or `Repository not found` (for a
   credentialed proxy fetch, the fixed label that output is reduced to). A
   `dns`, `network`, `timeout` or `host_key` failure is reported as it is today
   and never runs `gh`. For an install, only a transport failure goes on; a
   structural refusal (untrusted host, a checkout that cannot be moved aside, a
   pinned commit that did not land) is returned as it is.
4. Strict provider-binary mode (`KIROCREW_PROVIDER_BIN_STRICT`) is off. With it
   on, the fetch fails closed with `gh_strict_mode` before `gh` is resolved;
   see Non-goals.

### What runs

All `gh` commands are argv lists with no shell, a timeout, and the hardened
runner in `kiro_crew/github_runner.py` (validated absolute binary, `gh_env()`,
audit event before each spawn):

1. `resolve_gh()` finds a trusted `gh`. None found: cause `gh_missing`.
2. `gh api repos/<owner>/<repo> --silent`. Success goes straight to step 4.
3. Only when step 2 fails, `gh auth status --hostname github.com` names the
   cause: `gh_no_access` when it succeeds (GitHub answers 404 for a private
   repository the account cannot read), `gh_signed_out` otherwise. No probe
   uses `--active`, which older `gh` versions reject. A probe that runs past
   its limit ends with cause `timeout`.
4. The same git fetch runs again, through the split path `_git_fetch_ref`
   already uses for credentialed fetches: only the network `git fetch` gets the
   credential; `init`, `checkout` and `branch` run without it. For that one
   network step:
   - the git config gets `credential.helper=` (which clears every helper set
     before it) and then `credential.helper=!'<validated gh path>' auth
     git-credential`, through `GIT_CONFIG_*` variables, never argv;
   - the sandbox tier stays `strict`, and only `~/.config/gh` is lifted out of
     its masks (`extra_visible_dirs`), so `gh` can read its sign-in while
     `~/.ssh`, `~/.aws`, `~/.kube` and the rest of `_STRICT_DIRS` stay hidden;
   - the output is reduced to fixed labels, as for any credentialed fetch.

   git runs a `!` helper through `sh -c`. The string is fixed text plus the
   validated absolute path quoted with `shlex.quote`; no repository, index or
   user value is in it.

   Every step of this second attempt (init, remote setup, fetch, checkout)
   runs a git that `github_runner.validate_provider_executable` accepted, never
   the first `git` on `PATH`. Otherwise a planted `git` could write a URL
   rewrite to a custom remote helper into `.git/config` during init, and the
   credentialed fetch would honour it. No trusted git found: the retry stops
   before its first step.

The helper's environment. `gh auth git-credential` runs as a child of git, not
through `github_runner`, so the network step's env is built for it: the minimal
safe-key base the clone already uses, plus the keys `github_runner.gh_env()`
forwards (`GH_ENV_PASSTHROUGH`: `GH_CONFIG_DIR`, `GH_HOST` pinned to
`github.com`, proxy and CA settings, and `GH_TOKEN` / `GITHUB_TOKEN` when the
gateway's own environment sets them) and `DBUS_SESSION_BUS_ADDRESS` so `gh` can
reach the system keyring. When `GH_TOKEN` or `GITHUB_TOKEN` is set, `gh` uses
it in place of the stored sign-in, exactly as `gh` itself and every other `gh`
spawn in Kiro Crew do. Kiro Crew copies that variable from its own environment
to the child without reading, logging or storing its value, so Goal 2 holds for
it too.

Updates of an installed private app take the same path. Because a credentialed
pull inside an app checkout is already refused, the existing move-aside and
fresh-fetch path runs, as it does for a URL with embedded credentials.

### Integrity

The second attempt is the same git fetch with a different credential. So the
pinned-commit check, the origin and branch checks, admission of the cloned
`app.json`, containment of `subdirectory`, the hook and submodule switches, the
host trust gate (`is_clone_host_trusted`) and the app cache paths are all the
same code as for a public source. Nothing is unpacked from an archive, so no new
path-traversal surface appears.

### What the user sees

Four new fetch causes join the fixed set: `gh_missing`, `gh_signed_out`,
`gh_no_access` and `gh_strict_mode`. The registry status and the refresh result carry `repo` as
`owner/repo` next to `host` for these causes. The dashboard shows one sentence
per cause, for example:

> Sign in to GitHub CLI on the computer that runs Kiro Crew, with an account
> that can read acme/apps. Kiro Crew uses that computer's GitHub sign-in, not
> yours.

The second sentence matters for shared and remote crews, where the person
looking at the page is not at the gateway's computer. An install that fails
for these reasons returns the same sentence in `error` and the cause in `code`.

## Migration plan

One implementation PR, after this RFC is accepted.

- Phase 1: the gh second attempt for index fetch, card details, install and
  update; the four causes; dashboard sentences in every shipped language.
  Exit criteria, each a unit test with `gh` mocked:
  - a private index fetch that fails anonymously succeeds through `gh`, and the
    `gh` network step's env holds the helper config and no token;
  - `gh` missing, signed out, and 404 with `gh` each record their cause and
    never run the second git fetch;
  - a pinned install of a private app whose fetched commit differs from the pin
    is refused, exactly as for a public app;
  - an owner or repo name with shell characters, a port, or user info is
    refused before any process starts;
  - an index-originated install never calls `gh`;
  - only the network fetch gets the `~/.config/gh` lift, and its sandbox, read
    back through the real Linux plan and macOS profile, still hides `~/.ssh`,
    `~/.aws` and `~/.kube`; init, checkout and branch get no lift and no
    credential helper;
  - every step of the `gh` attempt runs the validated git, and none runs
    without one;
  - a first failure of `dns`, `network`, `timeout` or `host_key` never runs
    `gh`;
  - strict provider-binary mode refuses with `gh_strict_mode` before `gh` runs;
  - the dashboard renders the sign-in sentence with `owner/repo`.

## Backward compatibility

No config key, file format or route changes. A public registry takes the same
path as today. The cause set grows by four values; an older dashboard reads an
unknown cause as `other` and shows its plain line.

## Security considerations

- Token: Kiro Crew starts `gh`, and git starts `gh` as its credential helper.
  The token passes from `gh` to `git` over the credential protocol pipe. It is
  never in Kiro Crew's memory, argv, logs, caches or receipts.
- Confused deputy: the anonymous rule for index-chosen repositories is kept. A
  private index can list apps in its own repository (same-repo rule) and, for an
  `owner`-tier registry, elsewhere; any other private repository it names still
  fails, and the dashboard shows the plain failure.
- Sandbox: the network `git fetch` step stays in `strict` and gets back one
  folder, `~/.config/gh`. Nothing else the tier hides comes back: `~/.ssh`,
  `~/.aws`, `~/.kube` and the rest of `_STRICT_DIRS` stay masked for it. The
  widening this RFC asks for is that one folder, for that one process. The step
  runs no repository code: hooks and fsmonitor are off, its git is the
  validated one, and checkout is a separate step without the lift or the
  credential.
- The owner's pick. Least privilege wins where it works, and it does (see
  "Proof of the narrower sandbox" below). The earlier draft moved the step to
  `standard`, which would also have shown it the ssh, cloud and cluster
  folders. A reviewer who prefers that, or a different shape, can overrule this
  pick; the sandbox scope is the operator's to widen.
- Fail closed: any `gh` error, a timeout, or a failed audit write ends the fetch
  with a cause. No path goes back to an unchecked source.
- Strict provider-binary mode: the retry is refused before `gh` is resolved or
  any process starts (cause `gh_strict_mode`). This is a known limit, listed
  under Non-goals.
- No listener, route or storage exposure changes.

## Alternatives considered

- **Download a tarball with `gh api repos/<o>/<r>/tarball/<sha>`.** No git
  credential at all, but the tree is no longer a git checkout. The pinned-commit
  check would trust the server's word for the commit instead of git's object
  check, updates would need a second code path, and unpacking adds archive
  traversal risk. That is less checking for private sources, which Goal 3 rules
  out.
- **`gh repo clone`.** Runs git with the same `gh` credential helper, but
  outside Kiro Crew's split fetch, so checkout would run with the credential and
  without the hook switches.
- **Expose `~/.config/gh` to every https clone.** Simpler, but it would give the
  owner's GitHub sign-in to index-chosen clones too, which reopens the confused
  deputy hole.
- **Ask the user for a token.** Rejected by Goal 2 and Goal 4.
- **Move the network step to the `standard` tier.** This was the earlier draft.
  It works, but `standard` also unmasks `~/.ssh`, `~/.aws`, `~/.kube` and the
  rest of `_STRICT_DIRS` for that process. Rejected because the narrower lift
  below works.

### Proof of the narrower sandbox

The narrower option had two open points. Both were checked on 2026-11-18.

1. **`extra_visible_dirs` lifts the `strict` mask on `~/.config/gh`.**
   - Linux plan (`namespace_payload`): `strict` masks `~/.aws`, `~/.config/gh`
     and `~/.kube`. With the lift it masks `~/.aws` and `~/.kube`, and
     `hide_ssh` stays on.
   - macOS profile (`_build_seatbelt_profile`): `strict` has deny rules for all
     four folders. With the lift it has none for `~/.config/gh`, and the
     `~/.aws`, `~/.kube` and `~/.ssh` rules stay.
   - Live on Linux (real namespace launcher): in plain `strict`, `~/.config/gh`
     lists empty, `gh api user` fails, and `git ls-remote` of a private
     repository through the `gh` helper fails with "could not read Username".
     With the lift, `gh` signs in and the same `git ls-remote` lists the
     branch. `~/.ssh` still shows only `known_hosts`. In `standard` the child
     sees the whole `~/.ssh`.
2. **`gh` reaches the system keyring from inside `strict`.**
   - Linux: the `strict` and `standard` plans differ only in the masked
     folders, `hide_ssh` and the scrubbed environment prefixes. None of those
     touch `DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR` or `/run/user/$UID`.
     So the keyring is as reachable from `strict` plus the lift as it was from
     `standard`.
   - macOS: no tier has a rule for `~/Library/Keychains` or any mach service,
     so the login keychain is reachable in both.
   - Not run: a live sign-in that stores its token in a keyring. The test host
     stores it in `~/.config/gh/hosts.yml`.

Pinned by `test_the_gh_fetch_reads_the_gh_sign_in_and_no_other_credential_folder`
in #38999. It replays the network step's sandbox request into the real Linux
plan and macOS profile, and it fails when the step can read `~/.aws`, `~/.kube`
or `~/.ssh`.

## Open questions

- Should GitHub Enterprise Server hosts follow, keyed on the hosts `gh auth
  status` reports? Left for a later RFC. `gh_env(pin_host)` already takes a
  host, so it needs no change to this design.
- The narrower sandbox is the owner's pick (see Security considerations). A
  reviewer may overrule it.
