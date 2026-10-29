# Security

Do not report vulnerabilities through a public issue. Repository maintainers
should configure a private security contact or enable GitHub private
vulnerability reporting before publication.

Production deployments must set unique `SESSION_SECRET_KEY` and
`ADMIN_PASSWORD` values, use HTTPS, restrict filesystem permissions, and place
the application behind a maintained reverse proxy. Never commit `.env`, the
SQLite database, ASR token, consent PDFs, participant metadata, recordings, or
generated reports.

This is research software and has not received an independent security audit.
