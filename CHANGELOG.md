# Changelog

## Unreleased

## 0.8.2 - 2026-11-24

### Added

- external: opt-in quiet checks and silent done probes

## 0.8.1 - 2026-11-22

### Changed

- readme: align with tags, intake, and completed_at API

## 0.8.0 - 2026-11-22

### Added

- tasksmith: replace todo with tags, intake, and completed_at

## 0.7.2 - 2026-11-22

### Changed

- deps: declare ghdag as lower bound and install from git in CI

## 0.7.1 - 2026-11-21

### Changed

- readme: document EXTERNAL probes and todo.md

## 0.7.0 - 2026-11-21

### Added

- external: advance READY nodes that wait on external probes

## 0.6.1 - 2026-11-21

### Changed

- deps: pin ghdag to v0.152.2

## 0.6.0 - 2026-11-21

### Added

- todo: add todo.md registration and drift reporting

## 0.5.1 - 2026-11-21

### Changed

- tasksmith: rewrite README and pyproject as coordinator (#7529)

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