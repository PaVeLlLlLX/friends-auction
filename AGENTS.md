# Repository guidance

- Read `README.md` and `docs/specification.md` before changing auction behavior.
- Write documentation for readers who have not seen the development conversation. Name environments and prerequisites explicitly; exclude personal circumstances, conversational instructions and the state of a particular deployment.
- Describe implemented and verified behavior. Do not claim certification, security guarantees or standards compliance without evidence.
- Keep source identifiers and literals in English. Store Russian application messages in `static/ru.json` and test samples in `tests/fixtures/`.
- Use descriptive message keys. Keep test data out of runtime translation resources.
- Preserve the auction rules, privacy boundaries and existing user data. Use temporary databases and uploads in tests; never use the live `data/` directory for verification.
- Preserve unrelated local edits. Never commit credentials, databases, photographs, private logs or local notes.
- Run Python checks with Ruff and pytest, and frontend formatting checks with Prettier. Add regression tests for behavioral fixes; use browser checks for changes to interactive flows.
- Keep operational setup reproducible from a clean clone. Runtime dependencies belong in `requirements.txt`; development tools belong in `requirements-dev.txt` or `package.json`.
