# Contributing to Null

Thanks for your interest in Null. It's free and open source under
**Apache-2.0**, and contributions are welcome.

## Ground rules

- **Be honest with the data.** Null's whole promise is that it never fabricates
  a memory or a metric. Code that guesses where it should say "unknown" won't be
  merged.
- **Local-first.** A user's memory store is theirs. Nothing phones home.
- By contributing, you agree your contributions are licensed under Apache-2.0
  (the [DCO](https://developercertificate.org/) applies — sign your commits with
  `git commit -s`).

## Development setup

```bash
git clone https://github.com/alephnullai/null
cd null
python -m venv .venv && source .venv/bin/activate   # use a separate venv
pip install -e ".[embeddings]" pytest
```

> **Never point your live agent's memory at an editable checkout.** Every
> working-tree edit becomes live behavior mid-session. Develop in a separate
> venv with its own `NULL_DIR`; `null doctor` warns if you've crossed the wires.

## Running the tests

```bash
NULL_DIR=$(mktemp -d) python -m pytest -q
```

- Always isolate CLI/store tests with a throwaway `NULL_DIR` — without it you
  mutate your **live** store.
- Judge the run by its **exit code**, not the log tail.
- If you touch the embedding path, keep the model a process-wide singleton
  (`_MODEL_CACHE` in `embeddings.py`) — per-instance model loads OOM CI.

## Submitting changes

1. Open an issue first for anything non-trivial, so we can agree on the shape.
2. One logical change per PR. Add or update tests.
3. Keep the diff readable — match the surrounding style.
4. `git commit -s` (DCO) and open a PR against `main`.

## Reporting bugs / requesting features

Use the issue templates. A good bug report includes your OS, Python version,
`null doctor` output, and the exact command that failed.

## Security / privacy

If you find a way for Null to leak private data off-machine, **do not open a
public issue** — email `support@alephnull.ai` directly.
