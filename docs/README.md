# Kiro Crew Documentation

**This directory is the contributor and architecture documentation.** It is not
shipped to users: the docs that ship inside the Python package live in
[`../src/kiro_crew/docs/`](../src/kiro_crew/docs/README.md), and frontend-specific
docs live in [`../website/docs/`](../website/docs/README.md).

New here? Start with [guides/install.md](guides/install.md), then
[architecture/overview.md](architecture/overview.md).

## Where things live

| Directory | What it is for |
|---|---|
| [guides/](guides/README.md) | Install, run, and operate Kiro Crew. Task-oriented. |
| [architecture/](architecture/README.md) | How the system fits together, one doc per cross-cutting concern. |
| [build/](build/README.md) | Packaging, signing, and releasing. |
| [ci/](ci/README.md) | Everything that gates a pull request. |
| [app-kit/](app-kit/README.md) | Building apps that run inside Kiro Crew (third-party developer docs). |
| [design/](design/README.md) | Proposals for changes agreed before they are built. |
| [system-specs/](system-specs/README.md) | Change-control contracts. The doc a code change MUST update in the same commit. |
| [request-for-change/](request-for-change/README.md) | Proposals and decision records for large or contested changes. |
| [blog/](blog/README.md) | Essays on direction and design philosophy. Arguments, not contracts. |
| [reference/](reference/README.md) | Upstream documentation we mirror but do not author. |
| [task-specs/](task-specs/README.md) | Archived per-task specs. Not current context. |

## The rule for changing docs

A code change that alters documented behavior MUST update the docs **in the same
commit**. Concretely:

1. **Find the one owning doc.** Every subsystem has exactly one. The routing table
   in [`../AGENTS.md`](../AGENTS.md) maps subsystem to doc; `system-specs/modules/`
   is the usual home.
2. **Update it, do not add a second doc.** Prefer editing the existing doc over
   creating a new one. Two docs on one subject diverge, and then a reader cannot
   tell which is true.
3. **Update every index that points at it** when you add, move, rename, or delete a
   doc: this file, the directory's own `README.md`, and any doc that links to it.
4. **Do not write a changelog into a doc.** No `Last Updated:` line, no
   "previously/used to/we now" narration, no PR numbers or commit SHAs. Git records
   history; the doc states current behavior in present tense.
5. **Run the gate:** `./scripts/docs-lint.sh`. It fails on a broken internal link, a
   doc no index reaches, a directory with no index, a code comment citing a doc that
   does not exist, and a renamed doc whose filename is hardcoded in code.

Two constraints that are easy to miss:

- **`src/kiro_crew/docs/` filenames are an API.** That tree is packaged, is read at
  runtime by `tips.py` (gated by `tips_allowlist.py`), and specific filenames are
  hardcoded in dashboard Settings panels. Renaming a file there is a code change.
  The tree is also flat by design: `setup.cfg`'s `package_data` glob does not
  recurse, so a subdirectory would ship in the sdist but not the wheel.
- **User-facing docs belong in that packaged tree, not here.** An internal
  engineering note that lands in it ships to every `pip install`.
