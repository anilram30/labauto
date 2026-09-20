# Contributing to `labauto`

Thanks for looking. This is part of the [HF cable toolchain](https://github.com/anilram30/hf-cable-toolchain); the same conventions apply
across all seven packages.

## Getting set up

```sh
git clone https://github.com/anilram30/labauto.git
cd labauto
python -m venv .venv && . .venv/bin/activate
pip install "git+https://github.com/anilram30/cablecheck.git@main"
pip install -e ".[dev]"
pytest -q
```

## What a good change looks like

**Numerical code needs a numerical test.** Every method in this package is checked against something
independent — a closed-form result, a reciprocal transform, a synthetic case with known ground truth,
or a published reference value. A change that alters a number should either keep an existing test
passing or explain, in the pull request, why the new number is the correct one.

**The report is part of the source.** `docs/report.md` states the mathematics this package implements
and is built to `docs/report.pdf` in CI. If you change a method, change the report in the same pull
request. A method in the code that is not in the report is a bug in the report.

**Declare what is not modelled.** Every report has a limitations section, and it is there because
stating a method's boundaries is more useful than implying it has none. If your change introduces an
approximation, say so there.

## Style

`ruff check src tests` must be clean; the configuration lives in `pyproject.toml`. Beyond that the
house style is dense but commented: the comment explains *why*, and the docstring states the method
and its units. Notation in docstrings follows the report.

## Tests

`pytest -q` from the repository root. Tests must not need network access, an instrument, or any file
that is not in the repository. Anything slower than a few seconds belongs behind a marker.

## Commit messages

A short imperative subject line, and a body that explains the reasoning if the subject cannot.
