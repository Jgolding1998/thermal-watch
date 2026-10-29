# Security model

Thermal Watch is a local application for a trusted operating-system account. Its default mode is isolated simulation. It contains no provider accounts, shared API keys, telemetry, remote scripts, CDN assets, or private sample readings.

## Controls

- IPv4 loopback bind, exact allowed hosts and same-origin checks, and rejection of cross-site browser fetches.
- At most 16 concurrent connections, a five-second limit for request headers, and plain error replies with the same security headers. Versionless and unsupported HTTP versions are rejected, and responses do not name the Python version.
- A read-only dashboard: no upload, command execution, file browsing, settings mutation, or device control endpoint.
- Three explicit static routes. URL text is never joined to a filesystem path.
- Local JavaScript uses `textContent` for labels and events. Charts receive validated finite numbers.
- Content Security Policy, no-store responses, content-type enforcement, no framing, and disabled camera and microphone permissions.
- Bounded config and CSV inputs, finite temperature checks, strict sensor IDs, and timestamp validation. The built-in producer keeps a bounded atomic snapshot under an operating-system file lock.
- SQLite parameter binding, bounded retained readings and event rows, and explicit stale/fault states.
- External delivery needs both live CSV mode and the `--webhook` flag plus a user-provided environment value.
- Webhook transport uses a validated public HTTPS destination, pins one resolved IP with IPv4 preference, verifies the TLS hostname, ignores proxy settings, and does not follow redirects or retry another address.
- URLs, endpoint response bodies, and connection credentials are not stored in dashboard events.

## Remaining risks

A user or program that can write your local config, CSV, database, or Python installation can falsify readings or affect the process. Loopback is not a boundary against other processes running as your account. The service has no multi-user authentication or per-client rate limiting and is not designed for a public network. Do not change the bind address or put it behind a public tunnel.

An outbound endpoint can receive sensor labels and event times only when you deliberately enable it. Treat webhook URLs as secrets and use your own secret storage. The process pins DNS for its lifetime, so restart it if your legitimate endpoint's address changes. Delivery is one attempt, with failure retained locally; a failed endpoint does not stop sensor ingestion.

Transport construction and cleanup failures do not terminate the delivery worker. Shutdown waits a bounded interval for workers; a later result is not written after the application closes its delivery lifecycle. An attempt interrupted by shutdown can remain recorded as `local`, without a known delivery outcome, and is not retried after restart.

Clock rollback is shown as an input fault until a current reading arrives. A physical sensor can still report a believable wrong value. This application cannot establish calibration, prevent physical tampering, or guarantee that an alert reaches a person.

Database files, real readings, local config, screenshots of private installations, and environment files must stay out of Git. The included screenshots use simulated data only. Before publishing a fork, inspect both current files and Git history with a secret scanner and check commit-author privacy settings.

## Reporting a vulnerability

Use [Security > Report a vulnerability](https://github.com/Jgolding1998/thermal-watch/security/advisories/new) to send a private report. Do not submit vulnerability details through the public feedback form.

Include the affected version, platform, expected behavior and a synthetic reproduction. Leave out credentials, personal information and private files.
