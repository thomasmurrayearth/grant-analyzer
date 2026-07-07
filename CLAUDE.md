# Instructions for AI assistants working on this repository

## Project log — mandatory

`PROJECT_LOG.md` is the single human-readable record of what has been done
and where the project stands. **Every time you commit a change to this
repository, you must also update `PROJECT_LOG.md` in the same commit:**

1. Add a dated entry at the top of the Timeline (one to three plain-English
   sentences: what changed and why, plus the commit subject).
2. Update the "Where we are now" date and text if the change alters the
   app's status, architecture, or pipeline numbers.
3. Add, resolve, or amend "Open items" affected by the change.

A change is not finished until the log reflects it.

## Other standing rules

- **Generalise fixes:** prompt/safety-net changes must describe classes of
  programmes or behaviour — never hardcode a specific company (e.g.
  Thermify) or a specific funder seen in testing.
- **Deploying:** push to `main` auto-deploys to Railway. The owner is
  non-technical — "deploy" means push to GitHub and confirm Railway picks
  it up. Rollback is done from the Railway dashboard.
- **Verify before committing:** run the unit tests (`python3 -m unittest
  discover tests`) and exercise the change against the running app
  (`uvicorn main:app`). For UI changes, check the result in a headless
  browser; for output-quality changes, use the eval harness in `eval/`.
- **Frontend design system:** palette and type tokens live in
  `frontend/index.html` (tailwind.config + CSS custom properties). Reuse
  them; don't reintroduce generic navy/slate styling. Design changes must
  also carry through `manifest.json`, the icon set in `frontend/icons/`,
  and an `sw.js` CACHE_NAME bump so installed PWAs update.
