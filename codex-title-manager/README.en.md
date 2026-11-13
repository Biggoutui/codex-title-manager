# Codex Title Manager

A small Windows/Python 3.10+ community utility for a specific Codex desktop title-generation failure: an unavailable title model behind a custom provider. Not affiliated with OpenAI.

`catalog_repair.py` extracts a version-bound model catalog, removes one explicitly selected unavailable model, verifies the catalog with the installed app-server, and uses native version-checked configuration writes. It preserves pending ownership for uncertain commits and provides a minimal restore operation. Restart the desktop and test a **new** chat; catalog verification alone does not prove inference works.

`history_titles.py` previews a user-reviewed private JSON plan, applies batches of at most ten titles, and restores unchanged owned titles. It uses native APIs, not direct database access. Null-name chats are conservatively skipped. No automatic history summarization is included.

Install `requirements.txt`, then run either script with `--help`. Provide the exact desktop CLI via `--exe`; see the Chinese [README](README.md) for commands and limitations. Runtime state and backups live outside the repository by default. Never upload real rename plans, runtime state, credentials or configuration backups.

Run `python -m unittest discover -s tests -v`. Build a source ZIP with `python tools/build_release.py --output-dir ../release`. The builder uses an explicit file allowlist and scans both source bytes and ZIP entries. No uploading or telemetry is implemented. Experimental Codex APIs and embedded catalog formats may change; Windows integration and provider behavior require local validation.

MIT licensed project source; no Codex binaries or model catalogs are redistributed.
