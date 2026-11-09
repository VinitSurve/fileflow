# Contributing to FileFlow

Thank you for considering contributing to `fileflow`!

## Development Workflow

1. Clone the repository:
   ```bash
   git clone https://github.com/VinitSurve/fileflow.git
   cd fileflow
   ```

2. Set up the development virtual environment:
   ```bash
   make dev
   ```

3. Run the test suite:
   ```bash
   make test
   ```

4. Format and lint your changes:
   ```bash
   make format
   make lint
   make typecheck
   ```

5. Run full verification before submitting a PR:
   ```bash
   make check
   ```

## Pull Requests
- Ensure all tests pass.
- Maintain type annotations and documentation.
- Adhere to existing code formatting (Ruff) and typing standards (Mypy strict).
