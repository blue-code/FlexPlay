# Repository Guidelines

## Project Structure & Module Organization
`app.py` orchestrates Flask routes, FFmpeg helpers, edit tracking, and background APScheduler jobs, so keep new logic contained in helpers imported there. UI markup lives in `templates/index.html`, while CSS, JavaScript, and generated thumbnails stay under `static/`. Store sample or test media in `videos/` (or folders declared in `config.json`) and persist playback history in `history.json`. Future utilities should land in `services/` or `utils/` modules to keep the main app lean, and tests belong in `tests/` using the same import paths.

## Build, Test, and Development Commands
- `./setup.sh` — provisions the virtualenv, installs `requirements.txt`, and primes `static/thumbnails/`; rerun after dependency bumps.
- `./run.sh` — activates the env, checks folder health, and serves the app on `http://localhost:7777` for full-stack validation.
- `source venv/bin/activate && python app.py` — fast feedback loop for individual routes or scheduler jobs; run `ffmpeg -version` in the same shell to confirm codec availability.

## Coding Style & Naming Conventions
Follow PEP 8 with 4-space indentation and `snake_case` identifiers. Keep route handlers thin by delegating heavy lifting to helpers (e.g., `get_video_files`, `safe_join`), add concise docstrings for tricky flows, and prefer type hints for new utilities. Reuse existing HTML semantics and CSS classes inside `templates/index.html` so gradients, shortcuts, and responsive layouts stay intact.

## Testing Guidelines
Adopt `pytest` with Flask’s `app.test_client()` for endpoint coverage; mock filesystem or FFmpeg calls to keep runs deterministic. Place tests under `tests/`, mirror the module name in test files, and document any manual video checks (e.g., editing a large MKV). Run `pytest -q` before opening a PR and capture logs for regressions in background jobs.

## Commit & Pull Request Guidelines
Write small, imperative commits similar to `Add edit progress polling` so reviewers can follow intent. Every PR should include a summary, test evidence, screenshots or GIFs for UI changes, migration notes for `config.json`, and disclosure of new dependencies. Link related issues or TODOs and describe any scheduler or FFmpeg behavior changes to avoid conflicts with the cache cleanup task.

## Security & Configuration Tips
Never commit personal media, secrets, or a real `config.json`; update `config.json.example` instead. Always use `safe_join` or other vetted helpers when touching the filesystem, validate incoming parameters before invoking FFmpeg, and document new background schedulers so they coexist with existing maintenance jobs.
