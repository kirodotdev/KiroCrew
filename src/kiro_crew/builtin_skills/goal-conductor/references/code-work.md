# For code work

Read this when a goal's items change a repository. Your prompt's split,
owner and integrator rules hold as written; this names what they mean in code.

- **Cost.** Each worker on a repo costs a repo copy, a build and a CI and
  review round.
- **Parallel parts** are separate files. Two parallel parts that would both
  edit one file are one item. Shared files (version bump, changelog) belong to
  the integrator.
- **One PR from parallel parts.** A part's hand-over is its pushed branch and
  head sha. The integrator merges the branches, resolves conflicts and carries
  the `pr_merged` bar.
