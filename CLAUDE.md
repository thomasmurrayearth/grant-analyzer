# Instructions for AI assistants working on this repository

## How to give Thomas instructions — read this first

Thomas owns this app but is not a developer. Instructions written for a
developer are useless to him, and telling him to go and read a file is not an
instruction at all.

Every instruction must be:

- **Numbered, in order, one action per step.** No "then configure the secret" —
  say which button, on which page.
- **Self-contained in the message.** Never say "see LAUNCH_SETUP.md §3" or
  "follow the steps in the README". Paste the steps into the reply. Reference
  the file only as an afterwards-if-you-want-it.
- **Free of jargon.** Do not assume he knows what push, commit, pull, branch,
  repo, terminal, bash, CLI, environment variable, or secret mean. Say "send
  your changes to GitHub", "the Variables tab in Railway", "the file at
  C:\Users\thoma\grant-analyzer".
- **Given as full clickable URLs**, deep-linked to the exact page, not
  "go to Settings → Secrets".
- **Explicit about what success looks like** and what to do when it fails.

If a step depends on which tool he uses (e.g. how he sends changes to GitHub),
**ask him which one he uses** rather than guessing or writing three variants.


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
