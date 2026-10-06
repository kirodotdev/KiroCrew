# The Age 5 picture

The one picture a PR body may carry. `kirocrew-prepare-pr`'s Phase 1.5 points here rather
than restating it: the rules below are what an author needs while WRITING the
picture, and the skill file is what the loop reads on every load.


A picture is part of the body, not decoration on it. Draw one when the change has
a **shape** the reviewer would otherwise have to rebuild in their head from prose:
steps that moved, a state that changed hands, a guard that now passes or blocks
different inputs, a structure that gained or lost a field. Skip it for a one-line
fix, a rename, a test-only change, or a doc edit. **One picture, at most**, and a
picture that only restates section 3 is deleted, not kept.

You choose the form. Anything GitHub renders inside a ```` ```mermaid ```` fence is
fair — flowchart, sequence, state, class, ER, timeline, or whatever fits the delta —
and when the delta is a **matrix** (which inputs pass or fail, how each platform
behaves), a markdown table is the picture: rows are the concrete cases, columns are
Before and After, each cell is one coloured verdict. Do not force a matrix into
boxes and arrows. The constraints below make every PR read the same way at a glance.

- **Text, in the body.** A Mermaid fence or a markdown table, never an image — a
  rendered screen is a screenshot; see *Screenshots* below.
- **Before → After, and only the delta.** Two states side by side (two subgraphs,
  or two columns), or one graph where the changed edge is the only thing that
  stands out. Six to ten nodes, or eight rows, is the ceiling.
- **The diff palette is fixed.** In a Mermaid fence declare these four `classDef`s
  and tag every node; in a table write the matching word in each cell:

  | class | fill / stroke | table cell | meaning |
  |---|---|---|---|
  | `added` | `#DCFCE7` / `#16A34A` | `added` | new after this PR |
  | `changed` | `#FEF3C7` / `#D97706` | `changed` | behaviour changed |
  | `removed` | `#FEE2E2` / `#DC2626`, dashed | `removed` | gone after this PR |
  | `ctx` | `#E0F2FE` / `#0284C7` | `unchanged` | untouched, shown for context |

  The colour lives in the Mermaid fence, which renders the `classDef` fills; the
  table and the legend carry words. Do not put coloured squares (or any emoji) in
  the published body: the publish step rejects them, so an author who follows an
  emoji legend is refused at `gh pr create`. The words already carry the
  colour-to-meaning mapping. If you want the squares, keep them to a rendering
  that is not the published body.

  Colour the edges too: `linkStyle <n> stroke:#16A34A,stroke-width:2px` on the
  new path, `stroke:#DC2626,stroke-dasharray:4 3` on the removed one. Put one
  legend line under the picture: `added, changed, removed, unchanged` (map each
  word to its class above).
- **Caption in the Age 5 register**, one sentence: what now happens that did not,
  and what the reader sees because of it.
- **Place it inside section 3**, right after the paragraph it illustrates.

Check the render before pushing — `gh pr view <n> --web`. A fence that fails to
parse shows as a red error box, which is worse than no picture.
