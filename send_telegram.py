import os
from pathlib import Path
import requests

token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")
text = Path("scan_output.txt").read_text(encoding="utf-8", errors="replace")

# Alert only on a positive candidate or verification failure.
interesting = (
    "🟢 TRADE CANDIDATE" in text
    or "⚠️ UNABLE TO VERIFY" in text
)

if not interesting:
    raise SystemExit(0)

# Keep messages below Telegram's practical message-size limit.
text = text[-3800:]

url = f"https://api.telegram.org/bot{token}/sendMessage"
r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
r.raise_for_status()
print("Telegram alert sent.")
