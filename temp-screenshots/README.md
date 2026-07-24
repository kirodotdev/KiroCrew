# temp-screenshots/ — ephemeral PR review images

PR screenshots (the `prepare-pr` "Screenshots" section) are committed here, **not**
under `docs/` or `src/kiro_crew/**`.

Why this directory exists:

- **Never shipped.** This top-level dir is outside every packaged path — it is not
  in the Python wheel/sdist (`MANIFEST.in` prunes it) and not in the desktop DMG
  (the PyInstaller bundle only ships `src/kiro_crew/**` plus repo-root `agents/`
  and `skills/`). So review images can never ride into a shipped artifact.
- **Reviewable, then pruned.** Images are embedded in PR descriptions via
  commit-SHA-pinned `raw/` URLs, so they render during review. The weekly
  **Cleanup Temp Screenshots** workflow
  (`.github/workflows/cleanup-temp-screenshots.yml`) prunes files older than
  **14 days** from `main` (opening a cleanup PR, since `main` is protected).
  Existing PRs keep rendering: a SHA-pinned URL resolves from the pinned
  historical commit even after the file leaves `main`'s tip.

Do not rely on anything here long-term — treat it as a scratch area for review
artifacts only.
