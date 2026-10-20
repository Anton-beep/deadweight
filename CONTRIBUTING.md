# Contributing

Thanks for looking. This page is short on purpose: it lists the commands a change has to survive and the few rules that are not negotiable.

## Set up

```bash
uv sync
```

`uv` resolves and installs everything, including the development dependencies; there is nothing to `pip install` and no `requirements.txt` to keep in step. The tool has no runtime dependencies and must keep it that way: a change that adds one to `[project] dependencies` needs a very good argument in the pull request.

## The three gates

Run all three before you open a pull request, and before every commit that carries code:

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

All three are green in CI on libclang 16, 18, 20 and 22. A red gate is a red pull request, not a discussion.

## Rules that are not negotiable

- **Ruff is strict.** `select = ["ALL"]`, and every entry in `ignore` or `per-file-ignores` carries a justification next to it. A bare `# noqa` is rejected in review; a narrow `# noqa: CODE` with a comment saying why is accepted. Where `ctypes` defeats `mypy --strict`, use a narrow `# type: ignore[code]` and say why on the same line.
- **`mypy --strict` on `src`.** Tests are typed but not checked strictly.
- **Markdown is not hard-wrapped.** One paragraph, one line. That includes this file, the README and `CHANGELOG.md`.
- **Commits are conventional and atomic.** `feat:`, `fix:`, `docs:`, `test:`, `build:`, `ci:`, `refactor:`, `chore:`. One logical change per commit, and every commit passes the three gates.
- **Behaviour changes come with a test.** Verdict logic, exit codes and the report schema are the contract; a change to any of them without a test that pins the new behaviour will be sent back.
- **No committed build output.** `dist/`, `build/`, caches and run artifacts stay out of the tree.

## Changing the verdicts

The operator manual for the verdicts is part of the documentation set. If a change makes the tool propose something it did not propose before, that is a contract change: update the manual, the conformance corpus and the `CHANGELOG.md` entry in the same pull request.

Report a vulnerability privately, never in an issue; see [SECURITY.md](SECURITY.md).
