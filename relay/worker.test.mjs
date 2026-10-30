import test from "node:test";
import assert from "node:assert/strict";
import { handle, parseUpdate, inboxProperties } from "./worker.js";

const ENV = { WEBHOOK_SECRET: "s3cret-s3cret-s3cret", NOTION_TOKEN: "ntn_x", GITHUB_TOKEN: "ghp_x",
              OWNER_CHAT_ID: "777", NOTION_INBOX_DS: "ds-inbox", GITHUB_REPO: "me/uni" };

const post = (body, secret = ENV.WEBHOOK_SECRET) => new Request("https://relay.test/", {
  method: "POST", headers: secret === null ? {} : { "X-Telegram-Bot-Api-Secret-Token": secret },
  body: typeof body === "string" ? body : JSON.stringify(body) });

const textUpdate = (extra = {}) => ({ update_id: 5001, message: { message_id: 9, date: 1790000000, text: "buy a notebook",
  chat: { id: 777, type: "private" }, ...extra } });

// a fake fetch that records calls and answers like Notion/GitHub
function fakeFetch({ exists = false, notionStatus = 200, queryStatus = 200, githubStatus = 204 } = {}) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init, body: init.body ? JSON.parse(init.body) : null });
    if (url.includes("/query")) return new Response(JSON.stringify({ results: exists ? [{ id: "p" }] : [] }), { status: queryStatus });
    if (url.includes("api.notion.com/v1/pages")) return new Response("{}", { status: notionStatus });
    if (url.includes("api.github.com")) return new Response(null, { status: githubStatus });
    return new Response("?", { status: 404 });
  };
  return { impl, calls };
}

function captureLogs() {
  const lines = [];
  const original = console.log;
  console.log = (...a) => lines.push(a.join(" "));
  return { lines, restore: () => { console.log = original; } };
}

test("anything but POST just says ok", async () => {
  const { impl, calls } = fakeFetch();
  const res = await handle(new Request("https://relay.test/"), ENV, impl);
  assert.equal(res.status, 200); assert.equal(calls.length, 0);
});

test("a wrong or missing secret is refused before anything else happens", async () => {
  for (const secret of ["nope", null]) {
    const { impl, calls } = fakeFetch();
    const res = await handle(post(textUpdate(), secret), ENV, impl);
    assert.equal(res.status, 403); assert.equal(calls.length, 0);
  }
});

test("a missing setting is reported without touching Notion or GitHub", async () => {
  const { impl, calls } = fakeFetch();
  const log = captureLogs();
  const res = await handle(post(textUpdate()), { ...ENV, NOTION_TOKEN: "" }, impl);
  log.restore();
  assert.equal(res.status, 500); assert.equal(calls.length, 0);
});

test("garbage bodies get a 400", async () => {
  const { impl } = fakeFetch();
  assert.equal((await handle(post("not json"), ENV, impl)).status, 400);
});

test("messages from other chats or groups are ignored silently", async () => {
  for (const chat of [{ id: 999, type: "private" }, { id: 777, type: "group" }]) {
    const { impl, calls } = fakeFetch();
    const res = await handle(post(textUpdate({ chat })), ENV, impl);
    assert.equal(res.status, 200); assert.equal(calls.length, 0);
  }
});

test("photos and other unsupported messages are ignored", async () => {
  const { impl, calls } = fakeFetch();
  const update = { update_id: 1, message: { message_id: 2, chat: { id: 777, type: "private" }, photo: [{}] } };
  assert.equal((await handle(post(update), ENV, impl)).status, 200); assert.equal(calls.length, 0);
});

test("a text message is stored in the Notion Inbox and then wakes GitHub", async () => {
  const { impl, calls } = fakeFetch();
  const res = await handle(post(textUpdate()), ENV, impl);
  assert.equal(res.status, 200);
  assert.deepEqual(calls.map((c) => c.url), [
    "https://api.notion.com/v1/data_sources/ds-inbox/query", "https://api.notion.com/v1/pages",
    "https://api.github.com/repos/me/uni/dispatches"]);
  const [query, create, wake] = calls;
  assert.equal(query.init.headers.Authorization, "Bearer ntn_x");
  assert.equal(query.init.headers["Notion-Version"], "2026-03-11");
  assert.deepEqual(query.body.filter, { property: "Update id", number: { equals: 5001 } });
  assert.deepEqual(create.body.parent, { type: "data_source_id", data_source_id: "ds-inbox" });
  assert.equal(create.body.properties.Text.title[0].text.content, "buy a notebook");
  assert.equal(create.body.properties.Status.select.name, "Pending");
  assert.equal(create.body.properties.Received.date.start, "2026-09-21T14:13:20.000Z");
  assert.equal(wake.init.headers.Authorization, "Bearer ghp_x");
  assert.equal(wake.init.headers["User-Agent"], "uniagent-relay");
  assert.deepEqual(wake.body, { event_type: "wake" });
});

test("a voice note is stored with its file id and a placeholder text", async () => {
  const { impl, calls } = fakeFetch();
  const update = { update_id: 7, message: { message_id: 3, date: 1790000000, chat: { id: 777, type: "private" },
                                            voice: { file_id: "AwACAgQ", duration: 4 } } };
  assert.equal((await handle(post(update), ENV, impl)).status, 200);
  const props = calls[1].body.properties;
  assert.equal(props.Source.select.name, "voice"); assert.equal(props["File id"].rich_text[0].text.content, "AwACAgQ");
  assert.equal(props.Text.title[0].text.content, "(voice note)");
});

test("a caption on a voice note is kept as its text", () => {
  const m = parseUpdate({ update_id: 8, message: { message_id: 1, chat: { id: 1, type: "private" }, caption: " hi ", voice: { file_id: "F" } } });
  assert.equal(m.text, "hi"); assert.equal(m.source, "voice");
});

test("a repeated delivery of the same update is not stored twice", async () => {
  const { impl, calls } = fakeFetch({ exists: true });
  assert.equal((await handle(post(textUpdate()), ENV, impl)).status, 200);
  assert.equal(calls.filter((c) => c.url.endsWith("/pages")).length, 0);
});

test("if Notion fails the Worker answers 500 so Telegram retries, and does not wake GitHub", async () => {
  for (const opts of [{ queryStatus: 500 }, { notionStatus: 500 }, { notionStatus: 429 }]) {
    const { impl, calls } = fakeFetch(opts);
    const log = captureLogs();
    const res = await handle(post(textUpdate()), ENV, impl);
    log.restore();
    assert.equal(res.status, 500); assert.equal(calls.some((c) => c.url.includes("github")), false);
  }
});

test("if GitHub fails the message is still safe and Telegram is told ok", async () => {
  const { impl } = fakeFetch({ githubStatus: 403 });
  const log = captureLogs();
  const res = await handle(post(textUpdate()), ENV, impl);
  log.restore();
  assert.equal(res.status, 200); assert.ok(log.lines.some((l) => l.includes("wake_failed")));
});

test("long messages are cut to Notion's 2000-character limit", () => {
  const props = inboxProperties({ text: "x".repeat(5000), source: "text", updateId: 1, chatId: 1, messageId: 1, fileId: "", sentAt: "2026-01-01T00:00:00.000Z" });
  assert.equal(props.Text.title[0].text.content.length, 2000);
});

test("logs never contain message text, tokens or the secret", async () => {
  const { impl } = fakeFetch({ githubStatus: 500 });
  const log = captureLogs();
  await handle(post(textUpdate({ text: "my private plan: surprise party" })), ENV, impl);
  log.restore();
  const all = log.lines.join("\n");
  for (const forbidden of ["surprise", "ntn_x", "ghp_x", ENV.WEBHOOK_SECRET]) assert.equal(all.includes(forbidden), false, forbidden);
});

// ---- the single-file bundle that gets pasted into the Cloudflare dashboard
import { readFileSync } from "node:fs";
test("the dashboard bundle is self-contained: one default export, no imports", () => {
  const bundle = readFileSync(new URL("./dist/worker.bundle.js", import.meta.url), "utf8");
  const exports = bundle.match(/^export .*$/gm) || [];
  assert.deepEqual(exports, ["export default { fetch: (request, env) => handle(request, env) };"]);
  assert.equal(/^import /m.test(bundle), false);
});

test("the bundle behaves like the source", async () => {
  const bundle = (await import("./dist/worker.bundle.js")).default;
  const ok = await bundle.fetch(new Request("https://relay.test/"), ENV);
  assert.equal(ok.status, 200);
  const refused = await bundle.fetch(post(textUpdate(), "wrong"), ENV);
  assert.equal(refused.status, 403);
});
