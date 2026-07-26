# FarmBot integration — versioning

HACS shows an update only when `custom_components/farmbot/manifest.json`'s
`version` is bumped ahead of the last published release. This has already
been missed twice — see the `1.3.2` CHANGELOG entry (manifest stuck at
`1.2.0` across several released tags) and the `1.4.0` → `1.5.0` gap where a
whole options-flow refactor shipped with no version bump at all.

**Whenever a code or behavior change is made to this integration (not for
docs-only or formatting-only changes), bump the version before considering
the change done:**

1. Pick the new version:
   - **Patch** (`1.4.0` → `1.4.1`): bug fixes, internal refactors that don't
     change what a user configures or how services behave.
   - **Minor** (`1.4.1` → `1.5.0`): new features, new/removed options-flow
     fields, or any change to service behavior (gates added/removed,
     schema changes).
   - **Major** (`x.0.0`): breaking changes requiring user action (e.g. a
     required config migration).
2. Update `custom_components/farmbot/manifest.json` → `version`.
3. Add a dated entry to `CHANGELOG.md` describing what changed, following
   the existing entries' format (Added/Changed/Fixed/Removed).
4. Tell the user the exact version and tag name they need to create as a
   GitHub release (tags in this repo follow `VX.Y.Z`, e.g. `V1.5.0`) — do
   not push, tag, or create the release yourself; that's a user decision
   (see the repo-wide git safety rules).
