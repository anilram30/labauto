# labauto — the laboratory as a programmable system

**Runs a high-frequency cable laboratory as a programmable, traceable, self-validating measurement system.**

[![CI](https://github.com/anilram30/labauto/actions/workflows/ci.yml/badge.svg)](https://github.com/anilram30/labauto/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-25%20passing-brightgreen)](tests/)
[![Report](https://img.shields.io/badge/report-12%20pages-informational)](docs/report.pdf)

> **Part of the [HF cable toolchain](https://github.com/anilram30/hf-cable-toolchain)** — seven packages that take a high-frequency cable from a raw measurement to a predicted Ethernet link.
> 
> [A · cablecheck](https://github.com/anilram30/cablecheck)  ·  **B · labauto**  ·  [C · shieldeval](https://github.com/anilram30/shieldeval)  ·  [D · zprofile](https://github.com/anilram30/zprofile)  ·  [E · cableanalytics](https://github.com/anilram30/cableanalytics)  ·  [F · labplatform](https://github.com/anilram30/labplatform)  ·  [G · linktwin](https://github.com/anilram30/linktwin)

---

## The problem it solves

Laboratory measurements are usually driven by scripts that tell an instrument what to do and trust whatever comes back. `labauto` does something different: it knows what measurement is being performed, what a valid calibration is, which metadata is mandatory, which sanity checks must pass, when a measurement should be repeated, and whether the result is trustworthy enough to enter the engineering database at all. The whole chain — sample, barcode, procedure, instruments, calibration state, fixture, environment, measurement, validation, sealed archive — can be reproduced six months later from the archive alone.

## At a glance

|  |  |
|---|---|
| **Takes** | A barcode, a test procedure in TOML, and a laboratory of instruments — real or simulated |
| **Produces** | A sealed, hashed archive of the raw data with its full provenance, and validated results in the laboratory database |
| **Checked against** | Calibration and identity gates, replay of sealed archives, and an instrument simulator with a physically-based error model |
| **Technical report** | [`docs/report.pdf`](docs/report.pdf) — 12 pages, 29 references, every method stated with its mathematics and its limitations |
| **Tests** | 25, run against Python 3.11, 3.12 and 3.13 on every push |
| **Data** | Entirely synthetic. No proprietary or customer measurements are used anywhere in this toolchain. |

## Install

Python 3.11 or newer.

```sh
pip install "git+https://github.com/anilram30/cablecheck.git" \
            "git+https://github.com/anilram30/labauto.git"
```

Optional — `zprofile`, `cableanalytics` unlock extra capability, and the package works without them:

```sh
pip install "git+https://github.com/anilram30/zprofile.git" "git+https://github.com/anilram30/cableanalytics.git"
```

---

## What it does

`labauto` orchestrates a high-frequency cable laboratory: sample → barcode → procedure → instruments →
calibration state → fixture → environment → measurement → validation → raw-data archive → analysis
(projects A/D/E) → database, with the whole chain reproducible later from the archive alone.

It is not a VNA script. A **procedure** (TOML) says what a measurement *is*; a generic **engine** runs it as
a journalled state machine with gates and measurement-aware validation; **drivers** speak to instruments by
role through dialect tables; **simulators** of the analyser, climate chamber and ohmmeter let the whole thing
run — and be tested — without hardware, on the same code path, in simulated time.

```
CREATED → IDENTIFIED → INSTRUMENTS_READY → CALIBRATED → FIXTURE_READY → ENVIRONMENT_READY
        → MEASURED → VALIDATED → ARCHIVED → ANALYSED → RECORDED → DONE
          (ABORTED: a gate refused before measuring · REJECTED: quarantined · ERROR: journal kept)
```

## Quick start (simulated laboratory)

```bash
pip install cablecheck_projectA.zip      # project A (mandatory analyser; brings numpy/scipy/matplotlib)
pip install labauto.zip                  # this package;  pip install "labauto[visa]" for pyvisa transports
pip install zprofile.zip cableanalytics.zip   # optional analysers (projects D and E), used when installed

labauto init-demo mylab                  # lab.toml, registry, calibration record + check-standard reference, batch/sweep specs
labauto procedures --lab mylab/lab.toml  # built-in procedures, checked against this lab's instruments
labauto run --lab mylab/lab.toml mylab/batch_incoming.toml --dry-run   # walk every gate, measure nothing
labauto run --lab mylab/lab.toml mylab/batch_incoming.toml             # six samples: verify cal, measure, validate, archive, analyse
labauto run --lab mylab/lab.toml mylab/batch_next.toml                 # two pairs + NEXT in three hook-ups
labauto sweep --lab mylab/lab.toml mylab/sweep_hot.toml                # -40…125 °C, five sealed jobs, ~6 simulated hours
labauto archive --lab mylab/lab.toml list | verify | replay            # integrity and reproducibility of every sealed job
labauto cal --lab mylab/lab.toml list | policy | verify
labauto job --lab mylab/lab.toml <BARCODE> sparam-1000base-t1-pair --json
```

Everything lands under `mylab/`: `archive/<year>/<job>/` (raw `.s4p`, `.meta.json` sidecars, `aux.json`,
`journal.jsonl`, `procedure.toml`, `analysis/`, `manifest.json`), `lab.sqlite` (jobs, files, calibrations,
instrument log), `results.sqlite` (cablecheck's results database), `sweeps/*_summary.csv`, `logs/`.

## What the engine refuses, repeats and quarantines

| gate / check | what it is | what happens |
|---|---|---|
| identity | scan parsed (GS1-128 or `PART-LOT-SERIALc` with a mod-36 check character), resolved in the registry, cable type matches the procedure | `ABORTED` |
| capabilities | every required role present with ports / frequency / temperature / features the procedure needs | `ABORTED` |
| calibration | record for this instrument, type and ports; age ≤ `max_age_days`; \|T − T_cal\| ≤ `max_delta_t_k`; kit not overdue; **verified** against the check standard (ΔA, Δφ, RL) within `verify_every_hours` — verification is run automatically when needed | `ABORTED` |
| fixture | 2x-thru / files present (measured on request) | `ABORTED` |
| environment | ambient in window; for sweeps: chamber stable (band + duration + slope) then sample soak; label method recorded | `ABORTED` |
| grid, passivity, reciprocity, connection, trace noise, electrical length vs registry, IL vs DC loop resistance, IL envelope, far-end termination, instrument error queue | measurement-aware checks with value and bound in the sidecar | repeat up to `max_repeats`, then `flagged` or `REJECTED` (quarantine, no analysis, not in the engineering DB) |
| mandatory metadata | dotted paths the procedure requires must be present in the sidecar | `ERROR`, nothing sealed |

## Layout

```
src/labauto/
  clock.py         Clock / SimClock — time is a dependency
  scpi.py          transports (socket, VISA, simulator), SCPI short-form normaliser, instrument base
  drivers/         registry by role: vna (dialects: keysight-pna, rs-znb), chamber, dmm, scanner
  sim/             bench (samples with temperature physics, check standard), VNA / chamber / DMM simulators
  barcode.py       GS1 + internal labels, sample registry
  procedure.py     procedure model, loader, SHA-256
  procedures/      sparam-1000base-t1-pair, sparam-1000base-t1-two-pair-next, tempsweep-1000base-t1, sparam-generic-stp
  calibration.py   records, policy, verification against a check standard
  validation.py    the checks and the trust decision
  metadata.py      sidecar builder, mandatory-field enforcement, software provenance
  archive.py       sealed job directories, verify, replay
  analysis.py      analysers: cablecheck (A, mandatory), zprofile (D), cableanalytics (E)
  labdb.py         laboratory database (SQLite)
  lab.py           lab.toml → instruments, registry, calibration store, archive, database
  engine.py        the job state machine, batches, temperature sweeps, prompters
  demo.py          init-demo
  cli.py
tests/             25 tests: simulators through the drivers' real command strings, gates, trust, archive, replay, sweep, CLI
docs/report.md     the report (build with docs/build.sh → report.pdf); docs/make_figures.py regenerates the figures
```

## Real hardware

`lab.toml` with `driver = "vna.scpi"` and `address = "TCPIP::<host>::5025::SOCKET"` (or a VISA resource with
`labauto[visa]`), a chamber dialect or adapter, and the laboratory's own check-standard reference file in
`calibration/`. Bring-up order and what to verify first (the SNP data order of `CALC:DATA:SNP:PORT?`, the
cal-set catalogue quoting) are in the report, §9. Nothing here has run on a real instrument yet; the
simulators make the software testable, not correct by default.

## Honesty notes

Synthetic everything: samples, calibration residuals, noise, thermal behaviour. The models are stated in
the report and the constants are assumptions. Check bounds are engineering starting points. No uncertainty
budget is computed (the sidecar carries the ingredients). Single-site database; the multi-site platform is
project F.

---

## Contributing

Bug reports, questions about the methods, and pull requests are all welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md). Numerical changes need a numerical test, and a change to a method
is also a change to `docs/report.md`.

## Licence and attribution

MIT — see [LICENSE](LICENSE). Author: Sreeram Anil.

Built with AI assistance; the commit history records it. The engineering decisions, the validation
strategy and the limitations stated in the report are the substance of the work.

Part of the **[HF cable toolchain](https://github.com/anilram30/hf-cable-toolchain)** · [Report an issue](https://github.com/anilram30/labauto/issues) ·
[Changelog](CHANGELOG.md)
