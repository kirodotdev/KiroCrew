# Memory V2 evidence handoff

The [memory specification](../../../../system-specs/modules/memory-skills-hooks.md),
[security specification](../../../../system-specs/modules/security.md) and
[dashboard specification](../../../../system-specs/modules/learn-cron-dashboard.md)
own the current contracts. This archived handoff is an evidence index.

- [Algorithm report](algorithm-effectiveness-report.md) and
  [visual edition](https://github.com/kirodotdev/KiroCrew/blob/568abd2faf45db6ae36dd62164b8f5a85af74a71/docs/task-specs/2026/09/memory-v2/algorithm-effectiveness-report.html).
- [V1 versus V2, executive vision and six-page technical design](https://github.com/kirodotdev/KiroCrew/blob/aaf50f038995d4048ba18cf3039153729176e494/reports/f3f/README.md),
  available as PDF and editable Word documents against source f3f901e1.
  The source companion records claim mappings and historical measurements
  separately from the hosting commit. The [earlier report edition](https://github.com/kirodotdev/KiroCrew/blob/f3f901e143490e07acad12fec9fd391d08711a01/temp-screenshots/memory-v2/reports-fd5/README.md)
  remains available.
- [Evidence inventory](https://github.com/kirodotdev/KiroCrew/blob/23853b45f433ca0f0369498c46d4a0d212568b79/inventory.json),
  including historical source provenance, model outputs, media and review records.
- [Original UI captures and recordings](https://github.com/kirodotdev/KiroCrew/tree/568abd2faf45db6ae36dd62164b8f5a85af74a71/temp-screenshots/memory-v2).
- Supplied adversarial syntheses: [first review](https://github.com/kirodotdev/KiroCrew/blob/09968d7f5bcae6080b0f9df7066335ba0be93812/docs/task-specs/2026/09/memory-v2/adversarial-review-resolution.md)
  and [second review](https://github.com/kirodotdev/KiroCrew/blob/09968d7f5bcae6080b0f9df7066335ba0be93812/docs/task-specs/2026/09/memory-v2/fable-round-2-resolution.md).
  Full per-lane reports and unlisted findings were not supplied.

Each original receipt identifies the revision that ran. Evidence-publication
commits preserve those files; they do not establish that their own source was
executed. Older passing runs do not validate later changes. The algorithm corpus
is synthetic and is not a held-out V1/V2 comparison. The committed result JSON is
an integrity fixture, excluded from packages, not a new measurement.

The PR's current description and completed checks identify the accepted source,
review results and inspected UI evidence. CI owns product tests, builds, lint,
browser and model execution. Source review alone does not prove runtime behavior.
