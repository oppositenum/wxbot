# Existing Ubuntu volume compatibility

New installations use the repository-root `./deploy.sh`. It builds the full
Ubuntu 24.04 image for amd64 or arm64.

This Compose file remains only so an existing `wxbot-ubuntu-manual-home` volume
can be started without changing or deleting its saved WeChat login state. It
uses the same root Ubuntu image and no longer has a separate Dockerfile or
startup implementation.

It reads `../../.env` itself, including the management login, VNC password, and
runtime settings. Build the image after updating the checkout; its
compatibility Dockerfile refreshes all backend and web files inside the image
without bind-mounting source code. Recreate this compatibility container with:

```bash
docker compose -f docker/ubuntu-manual/compose.yaml up -d --force-recreate
```

After code changes, build the self-contained image before recreating it:

```bash
docker compose -f docker/ubuntu-manual/compose.yaml build ubuntu-wechat
```

Do not create a second `.env` in this directory. Keep the repository-root
`.env` when moving or redeploying this installation so the saved configuration
is reused.
