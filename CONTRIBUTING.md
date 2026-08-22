# Contributing

1. Create a branch from `main`.
2. Install the development dependencies with `python -m pip install -e ".[dev]"`.
3. Add or update tests for every policy or order-gate change.
4. Run `ruff check .` and `pytest` before opening a pull request.

Policy changes should document the safety rationale and explicitly call out any
change to the governance invariants in the README.

