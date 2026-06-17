# Deploying Polybot to Render (always-on)

This runs the bot 24/7 in the cloud so it keeps paper-trading even when your
Mac is off — which is what you need to collect enough resolved trades for a
trustworthy performance report (~100+, ideally 200+).

It's **one always-on Web Service** that serves the dashboard *and* runs the bot,
with a **persistent disk** so your data survives restarts.

> 💸 **Cost:** the always-on plan is **~$7/month** (Render's free plan sleeps
> when idle, which would stop the bot). Still paper trading — no real money is
> ever traded.

---

## Step 1 — Put the code on GitHub

Render deploys from a Git repo. From the project folder:

```bash
cd /Users/michael/Desktop/some_shit/polybot
git init
git add .
git commit -m "Polybot"
```

Then create an **empty private repo** on github.com (no README), and push:

```bash
git remote add origin https://github.com/<your-username>/polybot.git
git branch -M main
git push -u origin main
```

Your secrets are safe: `.env`, `data/` (the database), and `.venv/` are
gitignored and won't be uploaded.

---

## Step 2 — Create the service on Render

1. Go to **https://render.com** and sign up (you can log in with GitHub).
2. Click **New → Blueprint**.
3. Connect your GitHub and pick the `polybot` repo.
4. Render reads `render.yaml` and proposes the **polybot** web service + a 1 GB
   disk. Click **Apply**.

---

## Step 3 — Set your secrets

In the service's **Environment** tab, fill in the two values marked "set in
dashboard":

| Key | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your `sk-ant-…` key (for the AI layer) |
| `POLYBOT_PASSWORD` | a password you choose — you'll type it to open the dashboard |

The rest (`POLYBOT_AUTOSTART`, `POLYBOT_DB_PATH`, `PYTHON_VERSION`) are already
set by the blueprint. Save — Render will build and deploy.

> If you don't want the AI layer, leave `ANTHROPIC_API_KEY` blank and set
> `strategy.enable_llm_understanding: false` in `config.yaml` before pushing.

---

## Step 4 — Open it

When the deploy goes green, click the service URL
(`https://polybot-xxxx.onrender.com`). Your browser will ask for a login —
enter **any username** and the `POLYBOT_PASSWORD` you set.

The bot **auto-starts** on the server (no need to click ▶), so it's already
trading. Check the **Performance report** panel over the coming weeks; when it
reads `meaningful` / `solid` (~100–200+ resolved trades), the numbers are worth
trusting.

---

## Updating later

Push to GitHub and click **Manual Deploy → Deploy latest commit** in Render
(auto-deploy is off by default so a deploy never interrupts the bot
unexpectedly). The persistent disk keeps your trade history across deploys.

## Notes

- **One process on purpose.** The service runs a single web process so there's
  exactly one trading loop. Don't switch the start command to gunicorn with
  multiple workers — that would run multiple bots against the same database.
- **Data lives on the disk** at `/var/data/polybot.db`. To wipe and start fresh,
  delete the disk in Render (or open the dashboard and use a fresh run).
- **Still paper mode.** `mode: live` remains blocked in code; this deployment
  trades simulated money only.
