# Security Policy

## Scope and intended use
AS4PUR is an internal, LAN-only administration tool that wraps Netskope tenant operations. It is not designed or hardened to be exposed to the internet. Run it behind your own TLS reverse proxy on a trusted network, and read "Known limitations" in the README first. It is an unofficial community tool and is not affiliated with or endorsed by Netskope.

## Supported versions
Only the latest release is supported. Security fixes ship in a new release. Older tags do not get backports.

## Reporting a vulnerability
Please do not open a public issue for a security problem. Use GitHub private vulnerability reporting: open the Security tab of this repository and choose "Report a vulnerability". Include the version or commit, the steps you took, what you expected, what happened, and the impact. Do not include real tenant names, tokens or user data in a report. Redacted examples are enough.

This is a small project maintained in spare time. Reports are handled on a best-effort basis with no guaranteed response time.

## In scope
Flaws in this repository's code or default configuration, for example: authentication or session handling, exposure of API tokens or tenant data in responses, logs or error messages, injection, and unsafe defaults in the Docker deployment.

## Out of scope
- Vulnerabilities in Netskope's own products or APIs (report those to Netskope).
- Findings that need an already-compromised host or network.
- Exposing the app to the internet against the guidance above.
- Running it with more than one worker, which is unsupported.

## Handling credentials safely
Netskope API tokens are entered for each run and are not stored. If you believe this software exposed a token, revoke it in the Netskope console immediately, then report it here.
