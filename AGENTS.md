# Repo Workflow Notes

## Deployment Targets
- Local workstation is the source-of-truth working copy.
- Raspberry Pi hosts the same app stack and should be kept aligned with this repo before debugging production-only issues.
- SSH hosts are defined in `~/.ssh/config`:
  - `agility-ai` for the LAN Pi
  - `agility-ai-remote` for the Cloudflare tunnel path

## Guardrails For Future Codex Sessions
- Do not replace branch-aware auth, `User`/`Branch` routing, or customer/job context sync with older single-tenant code when merging PRs.
- Keep `upload_context`, `extracted_context_json`, and `matched_context_json` in `ProcessingSession`.
- Keep `SessionFeedbackEvent` and `feedback_reprocess_requested` schema defaults compatible with runtime column sync. New non-null columns need `server_default` when possible.
- Multi-file parsing must preserve unique per-file line IDs. Do not reuse bare `L1`, `L2`, etc. across files.
- `webp` is a supported upload type. Do not send it as `image/png`.
- When touching review feedback flow, avoid duplicate `SessionFeedbackEvent` rows for the same comment.

## Preferred Workflow
1. Inspect `git status` first. This repo may contain in-progress local work.
2. Compare against `origin/main` before porting PR changes.
3. Run local verification with `.venv\Scripts\python.exe -m unittest`.
4. Check the Pi runtime before deploying:
   - `ssh agility-ai` or `ssh agility-ai-remote`
   - inspect the deployed checkout, env file, and service status
5. Deploy by pulling the validated commit onto the Pi and restarting only the app service actually in use.

## Pi Verification Checklist
- Confirm current commit on Pi matches the intended Git commit.
- Confirm `.env` or service env still contains `OPENAI_API_KEY`, DB settings, and branch/auth settings.
- Confirm health endpoint returns OK after restart.
- Smoke test one CSV upload and one image/PDF upload if possible.
