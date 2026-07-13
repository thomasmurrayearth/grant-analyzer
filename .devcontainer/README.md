# Dev container — safe execution environment

Run all grant-analyzer work (including Claude Code) inside an isolated Docker
container. The container sees only this repository and the launch-plan folder
mounted at `/workspaces/launch-plan` — not the rest of the computer.

## One-time setup

1. Install **Docker Desktop** (running) and **VS Code** with the
   **Dev Containers** extension (`ms-vscode-remote.remote-containers`).
2. In OneDrive, right-click `Documents\Claude\Projects\Start-up consultant` →
   **Always keep on this device** (cloud-only files are invisible inside the
   container).
3. Optional — only needed to run the app itself inside the container: set
   `ANTHROPIC_API_KEY` (and `SUPABASE_URL` / `SUPABASE_KEY`) as Windows user
   environment variables. They pass through automatically. Never write keys
   into files in the repo.

## Every session

1. Open the `grant-analyzer` folder in VS Code.
2. `F1` → **Dev Containers: Reopen in Container** (first build takes a few
   minutes; fast afterwards).
3. Open a terminal, run `claude` (sign in once with `/login` using the Claude
   subscription), or use the Claude Code sidebar.
4. Tell Claude: **"Read /workspaces/launch-plan/GRANT_ANALYZER_LAUNCH_PLAN.md
   and execute the plan."** The WP status table in that file tracks progress
   across sessions.

## Why this is safe

- File access is limited to the repo and the mounted launch-plan folder;
  anything destructive is contained, and rebuilding the container resets it.
- That isolation makes relaxed permission prompts reasonable inside the
  container. Two cautions remain: the container has normal internet access
  (needed — the app does live web search), and `git push` to `main` still
  deploys to Railway, so keep the repo rule of running tests and reviewing
  changes before any push.

## Notes

- If the container build fails on the launch-plan mount (spaces in the path
  trip Docker on some setups), delete the `"mounts"` entry in
  `devcontainer.json` and copy the plan file in manually instead.
- Run the app with `uvicorn main:app --reload`; VS Code forwards port 8000
  to the browser automatically.
