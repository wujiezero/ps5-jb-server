# Releases and service updates implementation plan

**Goal:** A single application version, deterministic GitHub release assets, and authenticated web updates that preserve user state and recover from failed startup.

**Architecture:** `server/version.py` owns the SemVer constant. A tag workflow builds runtime-only tarballs plus SHA-256. `updates.py` discovers only official stable GitHub releases, downloads verified artifacts, persists status, and launches a separate systemd worker. The worker backs up and replaces application files only, restarts services and verifies `/api/version`; failures restore the previous application. The settings page reads status, checks and applies a specific displayed version.

**Deployment:** New installer writes root-owned `update-install.json` beside installed modules. It records exact runtime/static/config/data paths and systemd unit names. Source checkouts show versions and can check releases, but cannot self-update. Existing legacy installations can enable updates through an explicit CLI enrollment using their real configuration and unit names. No configuration, credentials, NAS keys, certificates, payload library or firewall setup is replaced by the updater.

## Steps

- [ ] Add version source, version endpoint and runtime artifact builder; test tag/version mismatch, excluded secrets and deterministic tarball entries.
- [ ] Add GitHub tag workflow (vX.Y.Z), Python/UI tests, tarball and checksum upload, generated release notes. Tag and source version must match.
- [ ] Add release discovery and download tests: SemVer comparison, drafts/prereleases ignored, missing release/asset/checksum, network/rate limit errors, strict URL scope, checksum mismatch, bounds, stale version and concurrent job refusal.
- [ ] Add detached update worker tests: archive traversal/symlink/duplicate/bounds rejection, metadata/tag equality, file backup/replacement, preserved config/key/data, restart health and rollback.
- [ ] Extend installer enrollment with fixed descriptor and `systemd-run` privilege boundary; retain existing layout compatibility.
- [ ] Add authenticated read/check/apply API routes; apply requires CSRF, no active transfers and a previously checked matching version. Persist status across restarts; no browser-supplied URL or path is accepted.
- [ ] Add settings card, sidebar version, explicit check/apply, release notes as text, rollback/error states and reconnect progress.
- [ ] Run Python suite, JS tests, artifact build/extraction checks and independent review; document release and legacy enrollment instructions. Publishing a real GitHub release waits until a concrete reviewed commit is ready.

## Required acceptance cases

- Current version is available without an external request; latest version never claims freshness after a failed check.
- Upgrades only target stable official release assets and checksum verification finishes before service replacement.
- User data stays byte-identical through successful upgrade and rollback tests.
- A killed/restarted management process cannot kill its update worker or erase its persistent status.
- Failed health verification restores the old version, reports rollback, and retains diagnostics.
- The initial release workflow exists without silently publishing unrelated unfinished game changes.
