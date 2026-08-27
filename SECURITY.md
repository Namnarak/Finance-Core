# Security

Finance Core stores financial data locally. Do not commit database files, tunnel credentials, VAPID private keys, webhook URLs, bot tokens, or push subscription endpoints.

Before publishing a fork, inspect the full Git history with a secret scanner and rotate any credential that may have been committed accidentally.

For security reports, use a private GitHub security advisory rather than a public issue.
