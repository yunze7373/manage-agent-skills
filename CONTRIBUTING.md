# Contributing

Keep the manager on-demand, minimally dependent, and non-destructive. New
runtime dependencies need a clear platform-specific reason.

For a new platform adapter:

1. Link to current primary documentation for discovery and enablement semantics.
2. Implement read-only discovery and dry-run before mutation.
3. Preserve unrelated configuration and create a backup.
4. Never delete installed skill files. Any relocation must be narrowly scoped,
   explicitly documented and tested for collisions and failed rollback.
5. Add isolated tests that do not touch the developer's real agent settings.

Run the unit tests and the skill validator before submitting changes.
