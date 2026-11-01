# Web demo

A browser example of the client upload flow. `server.mjs` stands in for your backend and holds the API key, while `index.html` captures a photo and sends it directly to FacePass with a session token.

You need Node 20.11 or newer, and the API running with `FACEPASS_CORS_ORIGINS=["http://localhost:3000"]`.

```bash
FACEPASS_API_KEY=fp_key_... node server.mjs
```

Open <http://localhost:3000>, enroll with your camera or a photo file, then verify.

This demo has no user login, so anyone who can reach it can act as any username. Your real backend must authenticate users before creating sessions for them.
