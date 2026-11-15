# Repository guidance

- Read `README.md` and `docs/specification.md` before changing auction behavior.
- Write documentation for readers who have not seen the development conversation. Name environments and prerequisites explicitly; exclude personal circumstances, conversational instructions and the state of a particular deployment.
- Describe implemented and verified behavior. Do not claim certification, security guarantees or standards compliance without evidence.
- Keep source identifiers and literals in English. Store Russian application messages in `static/ru.json` and test samples in `tests/fixtures/`.
- Use descriptive message keys. Keep test data out of runtime translation resources.
- Preserve the auction rules, privacy boundaries and existing user data. Use temporary databases and uploads in tests; never use the live `data/` directory for verification.
- Preserve unrelated local edits. Never commit credentials, databases, photographs, private logs or local notes.
- Annotate every Python function's parameters and return value, including callbacks, fixtures and tests. Describe function contracts in docstrings: purpose, non-obvious argument constraints, return semantics, side effects and failure behavior where relevant. Do not repeat the signature without explaining the contract.
- Describe JavaScript data structures and named functions with JSDoc. Keep API response types, UI state and DOM element types explicit; narrow values at untrusted boundaries. Static annotations do not replace runtime validation.
- Run strict mypy checks for Python and TypeScript `checkJs` for JavaScript. Do not silence failures with blanket `Any`, `@ts-nocheck`, ignored modules or disabled strictness; use a documented boundary assertion only where the application owns or validates the shape.
- Run Python checks with Ruff, mypy and pytest, and frontend checks with Prettier and `npm run typecheck`. Add regression tests for behavioral fixes; use browser checks for changes to interactive flows.
- Keep operational setup reproducible from a clean clone. Runtime dependencies belong in `requirements.txt`; development tools belong in `requirements-dev.txt` or `package.json`.
- Keep credentials and live storage out of container build contexts. Container changes require Compose validation and a passing container smoke job; use isolated volumes and fake credentials for verification.
