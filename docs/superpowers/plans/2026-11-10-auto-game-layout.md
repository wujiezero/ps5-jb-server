# Automatic game directory layout implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install one dumped PS4/PS5 game into exactly `<destination>/<title ID>` regardless of archive wrapper depth, using its own metadata and preserving streamed extraction.

**Architecture:** A pure game-layout module identifies a unique runnable root from the extracted manifest and captured small parameter files. Both extraction engines continue to share `archive.Installer`; only after validation does it rename the selected root into place. Registration and cheat handling are separate follow-up stages owned by the transfer manager and the local PS5Upload engine adapter.

**Tech Stack:** Python standard library, unittest, existing libarchive/libunrar adapters and fake PS5 file services.

---

### Task 1: Resolve and validate a game root

**Files:**
- Create: `server/game_layout.py`
- Create: `tests/test_game_layout.py`

- [ ] Add failing tests for a flat PS5 dump, PS4 SFO dump, deeply nested wrapper, sibling readme/PKG, conflicting title IDs, multiple distinct runnable roots, and package-only content.
- [ ] Use the behavioral contract below in assertions:

```python
layout = resolve_game(
    ['release/CUSA12345/eboot.bin', 'release/CUSA12345/sce_sys/param.sfo'],
    {'release/CUSA12345/sce_sys/param.sfo': sfo_bytes},
    hint='CUSA12345',
)
assert layout['root'] == 'release/CUSA12345'
assert layout['title'] == 'CUSA12345'
assert layout['platform'] == 'ps4'
```

- [ ] Run `../.venv/bin/python -m unittest discover -s tests -p 'test_game_layout.py'`; confirm the missing behavior fails.
- [ ] Implement bounded JSON/SFO metadata parsing and unique-root resolution. Require `eboot.bin` plus a valid `sce_sys/param.json` or `param.sfo`. Prefer validated metadata over filename hints; reject a metadata/hint mismatch instead of registering a different game silently. Return root, title, platform, version, and display name. Do not guess a runnable game for PKG-only content.
- [ ] Re-run the same command and confirm all cases pass.

### Task 2: Integrate into streamed extraction

**Files:**
- Modify: `server/archive.py`
- Modify: `tests/test_archive.py`

- [ ] Add failing tests using `FakePs5` and synthetic streamed entries for wrapper plus README, two wrappers, PS4 title from SFO, metadata mismatch, and independent merge semantics.
- [ ] Run `../.venv/bin/python -m unittest discover -s tests -p 'test_archive.py'`; inspect expected failures separately from environment skips.
- [ ] Capture bounded parameter-file contents at any wrapper depth during upload, record clean manifest paths, and resolve the game root before the final rename. Keep PKG-only extraction explicitly classified for a later native package-install stage; never describe it as a ready-to-play dumped game. Preserve existing rollback and merge behavior.
- [ ] Return structured `game` information with extraction result so follow-up registration and cheat stages use the real title/version/path.
- [ ] Run both targeted suites and `../.venv/bin/python -m unittest discover -s tests`; review the diff for both extraction engines, cleanup and bounded memory.

### Acceptance

- [ ] `eboot.bin` and `sce_sys` are direct children of the final title directory for every supported wrapper structure.
- [ ] Root ambiguity or mismatched identity fails before any existing game is replaced.
- [ ] The original game archive never needs to be written to the service disk.
- [ ] PKG-only content is not reported as a playable dump.
- [ ] Registration and cheats consume explicit metadata rather than infer identity from the destination name.
