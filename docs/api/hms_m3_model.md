# HmsM3Model

HCFCD M3 model HMS project discovery and extraction helpers.

## Safe project intake

`HmsM3Model.extract_project()` downloads into a unique intake folder, validates
the selected nested ZIP, and extracts the project into staging before publishing
the destination. A single common wrapper folder is removed. Unwrapped project
subdirectories are preserved. Paths containing traversal, drive or alternate
stream syntax, Windows device names, links, or colliding names are rejected.
Member reads verify ZIP CRCs and enforce the configured size limits.

The default limits are 512 MiB for the download and the selected nested ZIP,
2 GiB for the expanded project, 10,000 members per archive, and 32 path components
per member. These are configurable intake resource limits, not engineering model
limits. For a reviewed delivery that exceeds them, pass `max_archive_bytes`,
`max_uncompressed_bytes`, `max_members`, or `max_path_depth` explicitly. ZIP
directory parsing occurs before the member-count check, so memory requirements
also depend on the bounded archive size. The existing Requests socket timeout is
300 seconds; it is not a hard total-download deadline.

With `overwrite=False`, an existing ordinary destination directory is returned
without a download or a new integrity check. With `overwrite=True`, the existing
project stays in place throughout acquisition and extraction. Before replacing
it, the method writes an inventory containing file counts, byte totals and SHA-256
hashes. The previous directory is then retained under
`<output>/<model>/.<unit>.intake-<unique>/previous/`; it is not deleted.
`intake.json` in that folder records the prepared/completed disposition and both
inventories. Inspect and retain this record before explicitly retiring old data.

The output root must be writable only by trusted cooperating callers. An
exclusive `.<unit>.intake.lock` prevents simultaneous intake for the same unit.
A failed download or extraction leaves the prior destination unchanged and
retains its unique attempt folder for diagnosis. A failed publication attempts
to restore the prior destination; if restoration fails, both copies and the
inventory remain in the attempt folder and the error names the retained copy.

Directory replacement uses two renames, not an atomic filesystem transaction.
Process interruption between them can leave the destination absent and the old
project under `previous/`. Before recovery, confirm the writer has stopped, inspect
the attempt and inventory, then explicitly restore or complete the move. Only
afterward remove the stale lock. Failed attempt folders and prior copies have no
automatic retention cleanup; plan disk capacity for them. Archive checks do not
establish model readiness or run HEC-HMS.

::: hms_commander.HmsM3Model
    options:
      show_source: true
      heading_level: 2
      show_root_heading: true
      show_root_toc_entry: false
      members_order: source
