# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — 2026-09-18

### Added
- `labauto demo`: write a simulated laboratory and run its incoming-inspection batch in one command.
- Procedures: `downstream.report = false` skips HTML report generation; the result JSON is always
  written.

### Changed
- Simulated analyser: configurable serial and seed, a per-calibration residual signature, and a
  systematic transmission bias that scales with calibration quality — the ingredients the multi-site
  platform needs in order to see a site drift. Stronger defect profile.

## [0.1.0] — 2026-09-18

### Added
- First release: the laboratory operating system.
- Engine: journalled job state machine with gates, dry run, batches with resume, temperature sweeps.
- Procedures: four built-in TOML procedures, SHA-256 hashed, declaring sweep, calibration
  requirements, fixture, environment, files, checks and downstream analysis.
- Drivers: VNA with `keysight-pna` and `rs-znb` dialects, climate chamber, DMM and scanner, over
  socket, VISA or simulator transports, with an SCPI short-form normaliser.
- Simulators: cable bench with temperature physics, VNA with a residual-error and noise model,
  two-node thermal chamber, DMM.
- Calibration store, policy and verification against a check standard.
- Barcode parsing: GS1-128 with a GTIN check digit, and an internal scheme with a mod-36 check
  character.
- Validation checks and the trusted / flagged / rejected decision.
- Sealed archive with manifest verification and replay; laboratory database; analysers (`cablecheck`
  mandatory, `zprofile` and `cableanalytics` optional).
- CLI, 25 tests and a 12-page technical report.
