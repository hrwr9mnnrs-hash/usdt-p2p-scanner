# Cloud USDT P2P Scanner

This package is designed to run from GitHub Actions and be controlled from an iPhone.

Architecture:
GitHub Actions → Binance P2P → scanner → Telegram → iPhone

The workflow runs every 5 minutes when GitHub schedules it. Scheduled jobs can be delayed by GitHub, so this is an alert/research system, not guaranteed tick-level data.

## Setup

1. Create a GitHub repository.
2. Upload all files in this folder.
3. In the repo, open Settings → Secrets and variables → Actions.
4. Add:
   - TELEGRAM_BOT_TOKEN
   - TELEGRAM_CHAT_ID
5. Edit `.github/workflows/scan.yml` if you want a different capital.
6. Open Actions → USDT P2P Scanner → Run workflow to test immediately.
7. After the test succeeds, the scheduled scan runs automatically.

## Telegram bot

In Telegram:
1. Open @BotFather.
2. Send /newbot.
3. Follow the instructions.
4. Copy the bot token into the GitHub secret `TELEGRAM_BOT_TOKEN`.
5. Start a chat with your bot.
6. Use a Telegram getUpdates request in a browser or another method to obtain your chat_id, then store it as `TELEGRAM_CHAT_ID`.

Never put the bot token directly into code.

## Safety

This scanner is read-only. It does not place trades, release crypto, send payments, or withdraw funds.

The Binance P2P search endpoint may change or reject automated requests. Always re-check an alert manually in Binance before trading.
