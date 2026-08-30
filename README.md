# Finance Core

A private-first personal finance system built around **SQLite + MCP + a lightweight Web/PWA dashboard**. ChatGPT can act as the conversation layer, while the database remains the source of truth.

## What it does

- Natural Thai/English entries such as `ข้าว 75`, `เติมน้ำมัน 500`, `รับค่าทำเว็บ 1500`
- **Ambiguity Gate**: short or unclear entries are previewed and questioned instead of being silently guessed and saved
- Uses previous transaction history to improve categorization of familiar descriptions
- Integer-satang accounting; no floating-point money math
- Soft delete + audit trail + duplicate-write protection
- Daily / weekly / monthly / custom summaries
- Readable ledger output grouped by date
- Current balance, budgets, savings goals, recurring entries, subscriptions, debts and spending guards
- Month-end forecast, anomaly detection and financial alerts
- Web/PWA dashboard using the same SQLite database as the MCP server
- Optional web push notifications
- Optional OpenAI Secure MCP Tunnel deployment

## Example behavior

Clear entries can be saved directly:

```text
ข้าว 45
โออิชิ 15
โดเนทวี 183
```

Ambiguous entries are held for clarification:

```text
30
→ 30 บาทนี้เป็นค่าอะไรหรือซื้ออะไร?
→ รายการนี้เป็นเงินเข้า หรือเงินออก?

สหกรณ์ 30
→ ที่สหกรณ์ 30 บาท ซื้ออะไรหรือเป็นค่าอะไร?
```

A compact summary is formatted for quick reading:

```text
📊 วันนี้
รับ    +0.00 บาท
จ่าย   -183.00 บาท
สุทธิ  -183.00 บาท
รวม    900.32 บาท
```

## Repository layout

```text
mcp/   Finance Core database, MCP tools, CLI, tests and systemd units
web/   Web/PWA dashboard and push support
```

## MCP development

Requires Python 3.11+.

```bash
cd mcp
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
python -m unittest discover -s tests -v
```

For a production-style install with OpenAI Secure MCP Tunnel, provide your own tunnel ID and runtime credential. **No real tunnel ID or credential is included in this repository.**

```bash
export FINANCE_TUNNEL_ID='tunnel_...'
export CONTROL_PLANE_API_KEY='...'
cd mcp
./install.sh
```

## Web/PWA

The web app expects the Finance Core Python package to be available and reads the same SQLite database.

Typical environment:

```ini
FINANCE_DB_PATH=/var/lib/finance-mcp/finance.sqlite3
FINANCE_TIMEZONE=Asia/Bangkok
FINANCE_CURRENCY=THB
FINANCE_VAPID_SUBJECT=mailto:admin@example.com
```

The included `web/finance-web.service` is an example systemd unit. Adjust paths and user/group names for your deployment.

## Security model

This project is designed to be private-first:

- SQLite database files are ignored by Git
- VAPID private keys and push subscription data are ignored
- Tunnel runtime credentials are never part of source control
- The live deployment should be protected by a private network / Tailnet or another authenticated reverse proxy
- Keep backups encrypted and separate from the Git repository

See [SECURITY.md](SECURITY.md).

## Current version

Finance Core MCP: **v2.2.1**

The current v2.2 focus is **“แน่ใจค่อยลง / ไม่แน่ใจถาม”**: financial records should be accurate before they are convenient.
