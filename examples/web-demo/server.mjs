// Stands in for your application's backend: it holds the API key and makes the decisions.
// A real backend must authenticate the user before creating a session for them.
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { join } from "node:path";

const FACEPASS_URL = process.env.FACEPASS_URL ?? "http://localhost:8000";
const API_KEY = process.env.FACEPASS_API_KEY;
const PORT = Number(process.env.PORT ?? 3000);

if (!API_KEY) {
  console.error("Set FACEPASS_API_KEY to a key from `facepass apps create`.");
  process.exit(1);
}

async function facepass(path, options = {}) {
  const response = await fetch(`${FACEPASS_URL}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  return { status: response.status, body: await response.json() };
}

async function findOrCreateSubject(externalId) {
  const query = new URLSearchParams({ external_id: externalId });
  const found = await facepass(`/v1/subjects?${query}`);
  if (found.status !== 200) return found;
  if (found.body.items.length > 0) return { status: 200, body: found.body.items[0] };
  return facepass("/v1/subjects", {
    method: "POST",
    body: JSON.stringify({ external_id: externalId }),
  });
}

async function startSession({ action, username, consent }) {
  if (action === "identify") {
    return facepass("/v1/sessions", { method: "POST", body: JSON.stringify({ action }) });
  }
  const subject = await findOrCreateSubject(username);
  if (subject.status >= 400) return subject;
  return facepass("/v1/sessions", {
    method: "POST",
    body: JSON.stringify({ action, subject_id: subject.body.id, consent: consent === true }),
  });
}

function send(response, status, body) {
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(JSON.stringify(body));
}

async function readJson(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString() || "{}");
}

createServer(async (request, response) => {
  try {
    const url = new URL(request.url, `http://localhost:${PORT}`);
    if (request.method === "GET" && url.pathname === "/") {
      const html = await readFile(join(import.meta.dirname, "index.html"), "utf8");
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      response.end(html.replace("__FACEPASS_URL__", FACEPASS_URL));
    } else if (request.method === "POST" && url.pathname === "/api/sessions") {
      const { status, body } = await startSession(await readJson(request));
      send(response, status, body);
    } else if (request.method === "GET" && url.pathname.startsWith("/api/sessions/")) {
      const id = encodeURIComponent(url.pathname.split("/").pop());
      const { status, body } = await facepass(`/v1/sessions/${id}`);
      send(response, status, body);
    } else {
      send(response, 404, { title: "Not found" });
    }
  } catch (error) {
    console.error(error);
    send(response, 500, { title: "Demo backend error" });
  }
}).listen(PORT, () => console.log(`Demo running at http://localhost:${PORT}`));
