# Pi Workflow

## Hosts
- LAN: `ssh agility-ai`
- Remote tunnel: `ssh agility-ai-remote`

Both hosts are defined in the local SSH config and use the shared `id_ed25519` key.

## Standard Deploy Flow
1. Validate changes locally.
2. Push the branch or commit to GitHub.
3. SSH to the Pi.
4. `cd` into the deployed repo.
5. `git fetch --all --prune`
6. `git status`
7. `git checkout main`
8. `git pull --ff-only`
9. Restart the app service or container actually serving traffic.
10. Check logs and the `/health` endpoint.

## Things That Have Gone Wrong Before
- Merging older PR code over newer branch/auth/customer-context changes.
- Adding non-null columns without SQL defaults, which breaks runtime schema sync.
- Reusing line IDs across files in batch uploads.
- Breaking supported WebP uploads by sending the wrong MIME type.
- Creating duplicate `SessionFeedbackEvent` rows during review/reprocess flows.
