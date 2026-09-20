# Security Policy

## Supported versions

This project is a research and engineering toolchain, not deployed infrastructure. Fixes are applied
to the `main` branch; there are no maintained release branches.

## Reporting a vulnerability

Please report privately rather than in a public issue: use GitHub's
[private vulnerability reporting](https://github.com/anilram30/labauto/security/advisories/new), or email sreeramanil30@gmail.com.

Expect an acknowledgement within a week.

## Scope and threat model

`labauto` reads measurement data files, configuration files and, in some packages, instrument
responses. Treat all of these as untrusted input if they come from outside your organisation:

- Touchstone, CSV, TOML and JSON inputs are parsed without sandboxing.
- Archive and database paths are used as given; do not point the tools at a directory you do not control.
- No package in this toolchain executes code found in a data file, opens a network socket unprompted,
  or transmits measurement data anywhere.

Reports about parsing crashes on malformed input are welcome and are treated as bugs.
