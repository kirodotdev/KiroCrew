# Attribution

## Original app

**PPTX Maker** was written by **sktok**, as a standalone KiroCrew-family app
(`app-pptx-maker`, MIT-0). This directory is that app ported into the KiroCrew
open-source repository as a first-party builtin. The design is the original
author's: the Spec / Vibe / Style agent split, the deck-plus-library studio
layout, the animated SVG slide preview, the tabbed deliverable viewer that
follows whichever artifact the agent just wrote, and the style-versus-template
distinction all come from the upstream app.

What changed in the port is the plumbing, not the product:

- the standalone HTTP backend became in-gateway aiohttp routes under
  `/api/apps/pptx-maker/*`;
- the hand-written ESM UI became a normal React + TypeScript page using the
  dashboard's shared components and i18n catalog;
- the install-time patch of the engine's own prompt file was dropped in favour of
  an app-owned prompt resource, so the engine checkout stays unmodified.

`"author": "sktok"` in `app.json` reflects the original authorship and should
stay as it is.

## Presentation engine

Slide composition and `.pptx` writing are done by
[**spec-driven-presentation-maker**](https://github.com/aws-samples/sample-spec-driven-presentation-maker),
a public open-source project (AWS Samples, MIT-0). It is **not** vendored into
this repository: it is downloaded as a sha256-pinned source tarball into the app's
data directory on first use, unmodified, and driven through its own documented MCP
tools and Python API. The pin lives in `backend/engine_source.py`
(`ENGINE_TAG` / `ENGINE_COMMIT` / `ENGINE_TARBALL_SHA256`, bumped together), which
is the single place an engine upgrade is made.
