# Engineering conventions

Read by the factory's implementer and cited by reviewers. Keep this short:
mechanical rules belong in checks, tests, or CI.

## Conventions

- Keep behavior inside its owning module and update its system spec in the same change.
- Preserve the ACP provider seam; express harness differences through named capabilities.
- Keep tool authorization at the gateway and OS sandbox boundaries; do not route around them.
- Treat config, data-home, session, and wire formats as contracts with explicit owners.
- Follow the existing docs and test indexes; do not create a second source of truth.

## Smells to flag

- Duplicated code or policy that will drift from its owning module.
- Shotgun surgery: one behavior change forcing unrelated, scattered edits.
- A new abstraction without a current caller or a measured need.
- A harness-specific assumption leaking through a shared ACP interface.
- A security decision made in prompt text instead of an enforced boundary.
