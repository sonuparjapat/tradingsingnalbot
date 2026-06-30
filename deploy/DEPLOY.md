# Deploying the Bot to Oracle Cloud (Always Free)

Your local setup is untouched and remains the fallback. If anything here doesn't
work, just go back to running `python dev.py bot` on your laptop as before.

---

## Phase 1 — Create the Oracle Cloud account + server (manual, do this yourself)

### 1. Sign up
- Go to oracle.com/cloud/free and sign up.
- You'll need an email, phone number, and a card for identity verification
  (it will NOT be charged unless you explicitly upgrade to a paid account later).
- This step can take a few minutes to a day to get approved — be patient.

### 2. Create a Compute Instance (the actual server)
- Once logged into the Oracle Cloud Console, go to: **Compute → Instances → Create Instance**
- Name it something like `nifty-bot`
- Under **Image and shape**:
  - Click "Edit" → choose **Ubuntu 22.04** (or latest LTS) as the image
  - Click "Change shape" → choose **Ampere (ARM), VM.Standard.A1.Flex** →
    set 1 OCPU / 6GB RAM (well within the Always Free limits)
  - If Ampere shapes show "out of capacity," try a different region when
    signing up, or try again later — this is a known Oracle free-tier quirk.
- Under **Networking**: leave defaults (it will auto-create a VCN with a public IP)
- Under **Add SSH keys**: choose "Generate a key pair for me" and **download both
  the private and public key files** — you'll need the private key to connect.
- Click **Create**. Wait a few minutes for it to show "Running".
- Note down the **Public IP address** shown on the instance details page.

### 3. Open the firewall (only if needed)
Our bot makes outbound calls only (to Kite API, Telegram) — it does **not**
need any inbound ports open. You only need port 22 (SSH) open, which is
usually allowed by default. No extra firewall configuration needed for the bot itself.

---

## Phase 2 — Connect and upload files (do this yourself, or paste commands to me)

### 1. Connect via SSH
From your laptop (PowerShell):
```powershell
ssh -i "C:\path\to\downloaded-private-key.key" ubuntu@<PUBLIC_IP>
```

### 2. Upload the project files
From a **separate** PowerShell window on your laptop (not the SSH session):
```powershell
scp -i "C:\path\to\downloaded-private-key.key" -r "c:\Users\Sonu\Desktop\tradingsingnalbot\*" ubuntu@<PUBLIC_IP>:~/tradingsignalbot/
```
This copies everything — including `.env` (your secrets). That's expected;
it needs to live on the server to log in and trade.

---

## Phase 3 — Server setup (run on the SSH session)

```bash
cd ~/tradingsignalbot
chmod +x deploy/setup_server.sh
bash deploy/setup_server.sh
```

This installs Python, dependencies, and registers the bot as a systemd service
(auto-restarts on crash, auto-starts on server reboot).

---

## Phase 4 — Start and verify

```bash
sudo systemctl start nifty-bot
sudo systemctl status nifty-bot      # should show "active (running)"
tail -f ~/tradingsignalbot/bot.log   # watch live output, Ctrl+C to stop watching
```

Then check Telegram — you should get the "Bot working!" test message and the
startup message, exactly like running it locally.

Send `/status` in Telegram to confirm it responds quickly (within ~5 seconds).

---

## Useful commands going forward

| Action | Command |
|---|---|
| Stop the bot | `sudo systemctl stop nifty-bot` |
| Restart the bot | `sudo systemctl restart nifty-bot` |
| View live logs | `tail -f ~/tradingsignalbot/bot.log` |
| View error logs | `tail -f ~/tradingsignalbot/bot_error.log` |
| Re-upload updated code | repeat the `scp` command from Phase 2, then `sudo systemctl restart nifty-bot` |

---

## If something goes wrong

You don't need to fix the server — just go back to running locally:
```powershell
cd c:\Users\Sonu\Desktop\tradingsingnalbot
python dev.py bot
```
Nothing about the local setup was changed by this deployment.
