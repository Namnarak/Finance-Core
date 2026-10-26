# Finance Core MCP

The MCP/database component of Finance Core.

## Highlights

- SQLite WAL ledger with integer-satang accounting
- Thai/English short-entry parser
- Ambiguity Gate: unclear entries return questions instead of being saved
- History-assisted categorization
- Structured add/update/delete with audit trail
- Duplicate-write protection
- Accounts, budgets, savings goals, recurring items, subscriptions and debts
- Forecasts, spending guards, anomaly detection and alerts
- Readable daily/monthly summaries and ledger views

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
python -m unittest discover -s tests -v
```

## Secure MCP Tunnel install

The installer requires your own tunnel ID. It does not ship a project-specific tunnel identifier or API credential.

```bash
export FINANCE_TUNNEL_ID='tunnel_...'
export CONTROL_PLANE_API_KEY='...'
./install.sh
```

Runtime secrets are written outside the repository under `/etc/finance-mcp/`.

See the repository root README for the full Finance Core architecture and Web/PWA setup.

## Account setup for each installation

The public repository contains no preset bank or wallet accounts. Each user creates their own accounts and selects their own primary account via `set_primary_account(account="Your Account Name")`. The optional `FINANCE_PRIMARY_ACCOUNT` setting in a private environment can also name an existing account. Never commit your personal database, balances or runtime secrets. Without a chosen primary, no arbitrary first account is shown as the primary on summaries.
