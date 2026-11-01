# Integration guide

This guide is for developers connecting a website or mobile app to FacePass.

## Choose a flow

| | Backend upload | Client upload (session) |
|---|---|---|
| **Path of the photo** | Client → your backend → FacePass | Client → FacePass |
| **Credential** | API key on your backend | Short-lived session token on the client |
| **Best when** | You already proxy uploads, or the check runs server-side | You don't want photos passing through your servers |

**Never put an API key in a website or app.** Anyone can extract it and then read or delete your subjects.

## Client upload flow

```text
Client                 Your backend                FacePass
  │  start verification    │                          │
  ├───────────────────────►│  POST /v1/sessions       │
  │                        ├─────────────────────────►│
  │     session token      │◄─────────────────────────┤
  │◄───────────────────────┤                          │
  │  POST /v1/sessions/submit (photo)                 │
  ├──────────────────────────────────────────────────►│
  │     202 { status }     │                          │
  │◄──────────────────────────────────────────────────┤
  │                        │  webhook, or             │
  │                        │  GET /v1/sessions/{id}   │
  │                        │◄────────────────────────►│
  │    login decision      │                          │
  │◄───────────────────────┤                          │
```

The client never receives the match result, so a modified app can't fake one. **Make access decisions only from what your backend reads from FacePass.**

### 1. Create a session on your backend

Authenticate your user first, and only then create a session for that user's subject.

```bash
curl https://facepass.example.com/v1/sessions \
  -H "Authorization: Bearer $FACEPASS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"action": "verify", "subject_id": "b7c4…"}'
```

```json
{
  "id": "3f1a…",
  "token": "fp_ses_…",
  "action": "verify",
  "subject_id": "b7c4…",
  "expires_at": "2026-11-01T12:02:00Z",
  "max_attempts": 3
}
```

For enrollment, use `"action": "enroll"` and send `"consent": true` only after the person has agreed. For identification, use `"action": "identify"` with no `subject_id`.

### 2. Upload the photo from the client

Web:

```js
const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" } });
// ...attach the stream to a <video> element, then capture a frame when it's playing:
const canvas = document.createElement("canvas");
canvas.width = video.videoWidth;
canvas.height = video.videoHeight;
canvas.getContext("2d").drawImage(video, 0, 0);
const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));

const form = new FormData();
form.append("image", blob, "face.jpg");
const response = await fetch(`${FACEPASS_URL}/v1/sessions/submit`, {
  method: "POST",
  headers: { Authorization: `Bearer ${token}` },
  body: form,
});
```

Browsers require HTTPS for camera access, except on `localhost`. Add your site's origin to `FACEPASS_CORS_ORIGINS`.

Android (Kotlin with OkHttp):

```kotlin
val body = MultipartBody.Builder().setType(MultipartBody.FORM)
    .addFormDataPart("image", "face.jpg", jpegBytes.toRequestBody("image/jpeg".toMediaType()))
    .build()
val request = Request.Builder()
    .url("$facepassUrl/v1/sessions/submit")
    .header("Authorization", "Bearer $token")
    .post(body)
    .build()
```

iOS (Swift):

```swift
var request = URLRequest(url: facepassURL.appendingPathComponent("v1/sessions/submit"))
request.httpMethod = "POST"
request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
let boundary = UUID().uuidString
request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
var body = Data()
body.append(Data("--\(boundary)\r\n".utf8))
body.append(Data("Content-Disposition: form-data; name=\"image\"; filename=\"face.jpg\"\r\n".utf8))
body.append(Data("Content-Type: image/jpeg\r\n\r\n".utf8))
body.append(jpegData)
body.append(Data("\r\n--\(boundary)--\r\n".utf8))
let (data, response) = try await URLSession.shared.upload(for: request, from: body)
```

Flutter (`http` package):

```dart
final request = http.MultipartRequest('POST', Uri.parse('$facepassUrl/v1/sessions/submit'))
  ..headers['Authorization'] = 'Bearer $token'
  ..files.add(http.MultipartFile.fromBytes('image', jpegBytes, filename: 'face.jpg'));
final response = await request.send();
```

These native snippets show the request shape. They haven't been run in real apps yet.

**Handling the response:**

- **`202`:** the upload was accepted. Ask your backend for the decision.
- **`422` with a face or liveness code:** show `detail` and let the user retake the photo. The session allows `max_attempts` uploads.
- **`409` or `410`:** the session is over, so ask your backend for a new one.

**Camera tips:** capture JPEG (iOS HEIC isn't accepted), use the front camera, and keep the face at least 64 pixels wide. For better quality aim for about 640 pixels on the long edge or more. FacePass applies EXIF rotation and downscales large photos, so keep uploads under 8 MB.

### 3. Get the result on your backend

Poll `GET /v1/sessions/{id}`, or receive a webhook. Webhooks fire on `session.completed`, `session.failed` and `session.expired`:

```json
{
  "id": "6e0d…",
  "type": "session.completed",
  "created_at": "2026-11-01T12:00:40+00:00",
  "data": {
    "session_id": "3f1a…",
    "action": "verify",
    "status": "completed",
    "subject_id": "b7c4…",
    "result": { "match": true, "score": 0.6712, "threshold": 0.4 }
  }
}
```

The `result` shown here is shortened. For `verify` and `identify`, it has the same fields as the matching direct endpoint.

## Webhooks

An operator configures webhooks and receives the signing secret:

```bash
facepass apps set-webhook <application-id> https://your-backend.example.com/facepass/webhook
```

Verify every webhook before trusting it. The signature covers the raw request body, so check it before parsing the JSON:

```js
import { createHmac, timingSafeEqual } from "node:crypto";

function verifyFacePassSignature(rawBody, header, secret) {
  const fields = Object.fromEntries(header.split(",").map((part) => part.split("=")));
  const expected = createHmac("sha256", secret).update(`${fields.t}.${rawBody}`).digest("hex");
  const fresh = Math.abs(Date.now() / 1000 - Number(fields.t)) < 300;
  const a = Buffer.from(expected);
  const b = Buffer.from(fields.v1 ?? "");
  return fresh && a.length === b.length && timingSafeEqual(a, b);
}
```

- **Respond quickly:** return a 2xx within 10 seconds, and do slow work afterward.
- **Expect retries:** failures retry with exponential backoff, up to 8 attempts. Delivery is at-least-once, so ignore repeats of a `FacePass-Event-Id` you've already processed.
- **Don't redirect:** redirects are treated as failures, because signed payloads go only to the configured URL.

## Choosing verify or identify

Prefer **verify** whenever you know who the person claims to be, such as after a username or from a QR code. It's more accurate, and false matches don't grow with the number of enrolled people. **Identify** compares against everyone in your application, so its false match risk rises as you enroll more people. It uses a stricter threshold to compensate.

## Enrollment tips

Enroll 2–3 photos per person, in different lighting, to improve matching. A subject can have up to 5 faces by default, and each new face must match the ones already enrolled.

Before enrolling, tell people what you collect and why, and get their consent. See [security-and-privacy.md](security-and-privacy.md).
