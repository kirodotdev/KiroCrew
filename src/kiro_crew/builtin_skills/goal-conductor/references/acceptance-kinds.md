# Acceptance kinds

Read this before you write an acceptance object of a kind you have not used in
this goal, and whenever a verdict surprises you. The `acceptance` argument of
`work_ledger_record` lists the object for each kind, and `work_dispatch` takes
the same object: write one of those, exactly; there is nothing else to look up.

- **`pr_checks`**: a PR's checks all green via `gh`. Always set `repo` to
  `owner/name`: without it `gh` resolves the PR number against whatever
  checkout the evaluator runs in, which can be another repository's PR, and
  outside a checkout it cannot be evaluated. It names a NON-DRAFT pull
  request: a draft whose checks have not finished comes back `refused` rather
  than `pending` — the evaluator reads an unfinished check run on a draft as
  the author's turn, so no later cycle resolves it and you surface it instead
  of waiting. A draft whose checks have RESOLVED is judged on them like any
  other PR, so a green draft passes.
- **`pr_merged`**: a PR has merged, also via `gh`, with `repo` required. An
  open PR reads `pending`; one closed without merging reads `fail`.
- **`edition_check`**: `{"kind": "edition_check", "check": "<name>", "ref":
  "<id>"}`, a check an installed edition provides, for work that lands
  somewhere this evaluator cannot read itself, such as an edition's own code
  review system; the check names are the edition's to document. With no
  edition providing the check it answers `error`, and a check that says a
  person must decide answers `refused`.
- **`git_commit`**: `{"kind": "git_commit", "repo": "/absolute/repo",
  "branch": "<name>", "base": "<hex sha>"}`, for work that ends in a local
  commit with no pull request. Passes once `refs/heads/<branch>` holds a commit
  `base` does not (`git rev-list --count <branch> --not <base>`), and the
  evidence names the branch head. No such branch yet, or nothing past `base`,
  reads `pending`; a `base` the repository does not hold, or a path that is not
  a repository, reads `error`. Take `base` from `git rev-parse` before you
  dispatch. `repo` must be absolute: a relative one is `refused`, and the
  `accept_eval` tool screens the path as it screens a `file` path. Use this
  instead of asking the worker for a hand-over file: it checks the commit, not
  a report's existence. It does not look at the working tree.
- **`file`**: a path existing.
- **`human_approval`**: the user accepts it. Legitimate for design reviews and
  go/no-go gates, never machine-evaluated. How it is asked and settled:
  `references/rare-cases.md`.
