# Existing Ubuntu volume compatibility

New installations use the repository-root `./deploy.sh`. The root Dockerfile
is the only image definition and builds Ubuntu 24.04 for amd64 or arm64.

This Compose file remains only so an existing `wxbot-ubuntu-manual-home` volume
can be started without changing or deleting its saved WeChat login state. It
uses the same root Ubuntu image and no longer has a separate Dockerfile or
startup implementation.

It reads `../../.env` itself, including the management login, VNC password, and
runtime settings. Recreate this compatibility container with:

```bash
docker compose -f docker/ubuntu-manual/compose.yaml up -d --force-recreate
```

Do not create a second `.env` in this directory. Keep the repository-root
`.env` when moving or redeploying this installation so the saved configuration
is reused.
