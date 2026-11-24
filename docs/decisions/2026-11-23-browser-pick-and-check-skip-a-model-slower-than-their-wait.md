# A browser step's pick and check are not asked of a decision model that typically answers after their wait

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

A browser step's pick and check are not asked when the configured decision model's declared typical
answer time is above their 500 ms wait, and the step takes a timeout's path at once; a local model
declares its own measured median from the model table where that is slower than the shared local
figure (never lower), which also sizes Fast mode's longer wait; the "Browser steps: pick and check"
switch and its default stay as they are.

## Why

- The report expected that a provider which cannot answer inside the per-call wait is not asked, or
  that the wait follows the provider's speed. Every local model declared the same 400 ms, so Plumb-4B
  (about 2.4 s typical on a CPU) was asked for every pick and check, timed out at 500 ms, and its
  server still computed each answer nobody read.
- Using the model table's measured median needs no new setting, field or state, and the browser still
  reads only the provider's declared capabilities, never its name.
- Rejected: measuring the speed on the host, which needs new state and can follow if the table's
  figures prove wrong; turning browser steps off by default for slow models, which changes a default
  people already have; sizing pick and check from the slower time, which would add seconds before and
  after every click under the browser lock for a hint.
- Accepted limits: the median is a 10-core CPU figure, so Strands Decider 2B loses the share of picks
  and checks that fit 500 ms today, and Plumb-4B on an Apple GPU is not asked even where some asks
  would fit; a slow model behind a hand-written local address is still asked; Fast mode with
  Plumb-4B on a slow CPU fails after up to 5 seconds instead of 1.2.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/41196#issuecomment-7099848750 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/41196 -- the report.
