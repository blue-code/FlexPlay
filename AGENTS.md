# Repository Guidelines

## Project Structure & Module Organization
FlexPlay centers on `app.py`, which wires Flask routes, FFmpeg helpers, edit tracking, and APScheduler tasks. UI markup stays in `templates/index.html`, while CSS/JS plus generated thumbnails live inside `static/`. Video sources belong in `videos/` or the folders described in `config.json`, and playback state is persisted in `history.json`. Use `setup.sh` and `run.sh` to manage the virtualenv; if backend logic grows, break helpers into `services/` or `utils/` modules and import them explicitly from `app.py`.

## Build, Test, and Development Commands
- `./setup.sh` – creates/refreshes the `venv`, installs `requirements.txt`, and seeds `static/thumbnails/`; rerun after dependency changes.
- `./run.sh` – activates the environment, performs quick folder checks, and serves the app on `http://localhost:7777`.
- `source venv/bin/activate && python app.py` – faster debug loop for specific routes or background jobs; pair with `ffmpeg -version` to confirm transcoding support.

## Coding Style & Naming Conventions
Write Python that follows PEP 8: 4-space indentation, `snake_case` for functions/variables, and short, descriptive helpers (`get_video_files`, `safe_join`). Keep route handlers lean by pushing heavy work into helpers, document non-obvious flows with concise docstrings, and prefer type hints for new utilities. When editing `templates/index.html`, reuse existing HTML semantics and CSS classes so the gradient layout and keyboard shortcuts remain intact.

## Testing Guidelines
No automated suite ships yet, so create one whenever you touch routing, editing, or transcoding. Place tests in `tests/`, rely on `pytest` plus Flask’s `app.test_client()` for endpoint coverage, and mock filesystem/FFmpeg calls to keep runs deterministic. Execute `pytest -q` before submitting and summarize any manual video scenarios (e.g., editing large MKV) in the PR description.

## Commit & Pull Request Guidelines
Commits should be small and imperative—mirror the existing `Initial commit: FlexPlay - Flexible Video Player` style (e.g., `Add edit progress polling`). Each PR must include a summary, testing evidence, screenshots or GIFs for UI tweaks, migration notes for `config.json`, and a mention of new dependencies. Reference related issues or TODO items so reviewers can trace intent quickly.

## Security & Configuration Tips
Do not commit personal media, actual `config.json`, or secrets; update `config.json.example` instead. Always reuse helpers like `safe_join` when touching the filesystem, validate user input before invoking FFmpeg, and document any new background schedulers so they do not conflict with the existing cache cleanup job.
