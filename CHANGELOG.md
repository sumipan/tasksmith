# Changelog

## Unreleased

## 0.5.0 - 2026-11-21

### Added

- tasksmith: port human channel, CLI, and local loop (issue #7528)

## 0.4.0 - 2026-11-21

### Added

- core: port orchestration modules from nexus for issue 7527

### Changed

- Implement Issue #7527

### Fixed

- repair: drop out-of-scope conftest; colocate orchestration_env fixture (#7527)

## 0.3.0 - 2026-11-21

### Added

- core: add tasksmith foundation modules for issue 7526

### Fixed

- repair: drop CJK unicode escape from file-path regex (#7526)

## 0.2.3 - 2026-11-20

### Changed

- store: replace notes/html_notes with body on TaskStore

## 0.2.2 - 2026-11-20

### Changed

- store: slim TaskStore protocol and enrich local task views

## 0.2.1 - 2026-11-19

- `tasksmith.__version__` is now read from the installed package metadata instead of a hardcoded string.

## 0.2.0 - 2026-11-19

- Initial package layout with `TaskStore` port and `LocalTaskStore` file backend.