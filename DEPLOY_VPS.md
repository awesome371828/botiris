# Permanent bot hosting (VPS + Docker)

GitHub Actions runners are temporary and **cannot guarantee 24/7 uptime**. This deployment runs the bot on a persistent Linux VPS, starts it again after a reboot, and restarts the container if the bot process exits.

## Requirements

- Ubuntu 22.04/24.04 VPS (1 vCPU and 1 GB RAM is a reasonable starting point for this bot; choose more if the server runs other services).
- Docker Engine and the Docker Compose plugin.
- The GitHub repository cloned onto the VPS.

## 1. Install Docker on Ubuntu

Run on the VPS as a user with sudo access:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
```

If the package `docker-compose-v2` is unavailable on your image, install Docker Compose plugin using Docker's official Ubuntu instructions.

## 2. Get the project

```bash
git clone https://github.com/awesome371828/botiris.git
cd botiris
```

If the repository is already cloned, run `git pull` instead.

## 3. Configure secrets locally on the VPS

Create the environment file:

```bash
nano .env
```

Add your real values (do not commit this file):

```dotenv
TELEGRAM_BOT_TOKEN=PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE
SUPABASE_URL=PUT_YOUR_SUPABASE_URL_HERE
SUPABASE_SERVICE_KEY=PUT_YOUR_SUPABASE_SERVICE_KEY_HERE
BOT_OWNER_ID=6652898792
```

Save and lock down the file:

```bash
chmod 600 .env
```

Never paste bot tokens or Supabase service keys into chat or commit them to GitHub.

## 4. Start it and enable boot persistence

```bash
sudo docker compose up -d --build
sudo docker update --restart unless-stopped pear-cm
sudo docker logs --tail=100 -f pear-cm
```

Docker's service is enabled at boot, and `restart: unless-stopped` starts the bot after reboots and retries after process exits. To check status:

```bash
sudo docker compose ps
sudo docker inspect -f '{{.State.Status}} | restarts={{.RestartCount}}' pear-cm
```

Update after a repository change:

```bash
git pull
sudo docker compose up -d --build
```

Stop it:

```bash
sudo docker compose down
```

## Important: avoid two copies polling Telegram

The existing GitHub Actions workflow also runs the bot. Telegram polling commonly fails when two copies of the same bot token run at once. **After the VPS copy starts successfully, disable the GitHub Actions bot workflow** at GitHub → repository → Actions → “Run Pear CM” → ⋯ → Disable workflow. Do not disable the CI tests workflow. Also stop any other hosting instances using this token.

## What this does and does not guarantee

This is persistent hosting, not a trick to keep a GitHub runner alive. It recovers from process exits and host reboots, but no provider can guarantee zero downtime: a VPS can have network/provider outages, invalid credentials, API errors, or application bugs. Check logs after the first start and verify that the bot responds in Telegram.
