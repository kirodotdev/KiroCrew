# Kiro Crew onboarding demo (static build)

A static build of the real Kiro Crew dashboard. It walks through the first-run
experience with no gateway, server or local install behind it. It exists for
usability testing of onboarding: send a tester a link and they get the same
screens a brand-new user sees today.

This branch is a test harness, not a feature. It is not meant to merge.

## What a tester goes through

1. **Set up Kiro**: the real setup screen ("Kiro CLI isn't installed"), with the
   real install command and the "Use other coding agents" list. Each
   **Check again** moves one step forward, standing in for installing and then
   signing in: not installed, then installed but signed out, then ready.
2. **Import setup**: finds Claude Code and Codex on the "host". The tester picks
   sources, reviews, imports, and sees a summary.
3. **Privacy**: unchanged.
4. **Customize**: the look picker, then the "about you" questions.
5. **Ghost tour**: the guided popovers over Schedule, Discover and "Start your
   first session".
6. **Chat**: anything the tester sends gets one canned reply, which streams in
   like a real one. The session takes its title from the first message.

Everything else in the app loads too, but off this path pages show up empty.
For example, Discover lists no apps.

## Links to give testers

| URL | What it does |
| --- | --- |
| `https://<host>/?reset` | Fresh first run. **Use this for every new tester.** |
| `https://<host>/?reset&setup=ready` | Fresh first run, starting after the setup screen. |
| `https://<host>/` | Picks up where this browser left off. |

State is kept in the browser's localStorage, so a reload resumes, and two testers
on two machines never see each other's data. `?reset` clears it all, including
the dashboard's own first-run flags, then strips itself from the URL.

## How it works

The demo build runs the same app code as the real dashboard. Only the entry point
and the network layer differ.

- `vite.demo.config.ts` reuses the stock `vite.config.ts` and changes four things:
  - It builds into `dist-demo/` instead of `dist/`.
  - Its entry is `src/demo/boot.ts`, not `src/main.tsx`.
  - It drops the plugins that only matter when the gateway serves the build.
  - It removes the service-worker registration and ships the logo file.
- `src/demo/boot.ts` installs the mocks first, then imports the real `main.tsx`.
- `src/demo/transport.ts` answers every same-origin `/api/*` request inside the
  page, by replacing `window.fetch`, `WebSocket` and `EventSource`. Static assets
  still load normally.
- `src/demo/routes.ts` maps each request to an answer. The first-run path has
  scripted answers: setup status, import scan and apply, the theme and onboarding
  flags, config writes, and sessions and send. Every other request gets an empty
  answer in the right shape, so a surface off the path renders empty rather than
  erroring.
- `src/demo/fakeSocket.ts` stands in for the dashboard's `/api/ws` socket.
  `src/demo/reply.ts` pushes the canned reply over it, in the same frame order a
  real turn uses (`chat_status`, `chat_chunk`, `chat_message`, `chat_done`).
- `src/demo/state.ts` holds the fake gateway state in localStorage and handles
  `?reset`.

To see which calls hit the generic answer, run `__demoMisses()` in the browser
console. If a screen looks wrong, a missing route is the usual cause: add it to
`ROUTES` in `src/demo/routes.ts`.

The stock build (`npm run build`) does not change.

## Building the static files

You need Node 24 (see `/.nvmrc`; the minimum is 22.12) and a full clone of the
repository. The build reads the logo from `src/kiro_crew/static/`, outside
`website/`.

```bash
cd website
npm ci
npm run build:demo        # writes website/dist-demo/
npm run preview:demo      # optional: serve it at http://127.0.0.1:4173
```

`build:demo` runs a type check, then the Vite build. It needs up to about 6 GB of
memory.

To make a zip with `index.html` at its root:

```bash
cd website/dist-demo
zip -rq ../kirocrew-demo.zip . -x '*.gz' '*.br'
```

`-x '*.gz' '*.br'` leaves out the pre-compressed copies of each asset that the
build also writes. Static hosts compress on their own, so the copies are dead
weight: about 80 MB uncompressed, and about 33 MB once zipped.

The build expects to be served from the root of a domain. For a sub-path, build
with `KIROCREW_BASE_PATH=/your/path npm run build:demo`.

## Deploying on AWS Amplify Hosting

Either option needs the SPA rewrite below. Without it, a refresh on a route such
as `/schedule` returns 404.

### Option A: manual deploy (simplest)

1. Build and zip as shown above.
2. In Amplify, choose **Create new app**, then **Deploy without Git**, and upload
   `kirocrew-demo.zip`.

### Option B: connect this branch

`/amplify.yml` at the repository root is a monorepo build spec for this branch. It
installs Node 24, runs `npm ci` and `npm run build:demo` in `website/`, and
publishes `website/dist-demo`.

1. Connect the repository in Amplify and choose this branch.
2. When asked whether it is a monorepo, set the app root to `website`. This sets
   the `AMPLIFY_MONOREPO_APP_ROOT=website` environment variable.
3. Use a build instance with at least 8 GB of memory (the standard size).

This spec has not been run on Amplify yet. If the build fails, use Option A.

### Rewrite rule (required)

Under **Hosting**, then **Rewrites and redirects**, add:

| Source address | Target address | Type |
| --- | --- | --- |
| `</^[^.]+$|\.(?!(css|gif|ico|jpg|jpeg|js|mjs|png|txt|svg|woff|woff2|ttf|otf|map|json|webp|wasm|webmanifest)$)([^.]+$)/>` | `/index.html` | `200 (Rewrite)` |

## Notes and limits

- The link is public, with no sign-in. The build contains only the open-source
  dashboard and canned data: no credentials, and no calls to any real backend.
- Chat never reaches a model. Every message gets the same reply.
- Pages off the onboarding path load but are empty, and their actions do nothing.
- The setup screen shown first is the "Kiro CLI not installed" variant. What a
  real new user sees there depends on how they installed Kiro Crew.
