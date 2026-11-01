# Security and privacy

This guide is for whoever deploys FacePass or is responsible for compliance. It isn't legal advice.

## What's stored

| Data | Stored? | Retention |
|---|---|---|
| Uploaded photos | No. Held in memory during the request only | Discarded right away |
| Face embeddings | Yes, in plain form so Postgres can search them | Until deleted through the API |
| `external_id` | Yes | Until the subject is deleted |
| Consent timestamp | Yes, per face | Deleted with the face |
| Session outcomes and webhook records | Yes, including match scores | 7 days after finishing |
| Audit events (no scores or biometrics) | Yes | 90 days by default, and they outlive deleted subjects |
| Logs | Method, path, status, duration and request id. Subject and session ids appear in paths | Your log platform's policy |

Embeddings are biometric data even without the photo. Treat the database as highly sensitive.

## Built-in protections

- **Tenant isolation:** every query is scoped to the application, and a composite foreign key stops a template from pointing at another application's subject.
- **Hashed tokens:** API keys and session tokens hold 256 random bits and are stored only as SHA-256 hashes.
- **Encrypted webhook secrets:** encrypted with a key derived from `FACEPASS_SECRET_KEY`. Changing that key means resetting webhooks with `facepass apps set-webhook`.
- **Client results stay private:** clients never see match results, and sessions are single-outcome with limited attempts.
- **Brute-force limits:** per-key rate limits, plus a temporary lock on a subject after repeated failed verifications. An API key holder can trigger this lock deliberately, which is an accepted trade-off.
- **Enrollment protection:** a new face must match the subject's existing faces.
- **Upload safety:** files are checked by content rather than declared type, with limits on bytes and pixels checked before decoding.

## Your responsibilities in production

**Infrastructure:**

- Put the API behind a reverse proxy that terminates TLS. The compose file binds to localhost, and plain HTTP must never carry face data across a network.
- Make sure your proxy forwards `Content-Length`, since requests without it are rejected. Limit the upload size there as well.
- Run Postgres with encrypted disks and backups, restrict network access, and use a strong unique password.
- Keep `FACEPASS_SECRET_KEY` in a secrets manager.
- Deleted subjects stay in backups until those expire, so set a backup retention period and disclose it.
- Set `FACEPASS_ENVIRONMENT=production`, so webhook URLs must use HTTPS.

**Trusted operators:** webhook URLs are set by operators through the CLI and aren't filtered for internal addresses. If you ever expose this to customers, add protection against server-side request forgery first.

**Authentication strength:** don't use face matching as the only factor for payments, account recovery or anything high-stakes. Liveness isn't validated (see [evaluation.md](evaluation.md)), and a client can upload any photo. Combine it with a second factor, and provide a non-biometric alternative for people who can't or won't use it.

## Legal compliance

Face data is regulated as biometric data in many places, including under the EU and UK GDPR and Illinois BIPA. Get legal advice for your launch regions. Typically you'll need to cover:

- **Consent:** explicit and informed, recorded before enrollment. `consent=true` is your assertion that this happened, and FacePass only records the time.
- **Notice:** what you collect, why, how long you keep it and who processes it.
- **Deletion:** `DELETE /v1/subjects/{id}` erases a person's face data. Call it when users withdraw consent or close their account. FacePass has no automatic expiry for embeddings, so schedule deletion for inactive users.
- **Impact assessment:** GDPR generally expects one for biometric identification.
- **Identify:** recognizing people in public or without their knowledge is restricted or banned in some places, including under the EU AI Act.

## Reporting a vulnerability

Report suspected vulnerabilities privately to the repository owner, not in a public issue.
