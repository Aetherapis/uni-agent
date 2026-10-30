// Prints the Notion rows the Worker would create for sample Telegram updates (used by the Python tests
// to check the Worker and the agent agree on the Inbox format).
import { handle } from "./worker.js";

const ENV = { WEBHOOK_SECRET: "s", NOTION_TOKEN: "t", GITHUB_TOKEN: "g", OWNER_CHAT_ID: "777", NOTION_INBOX_DS: "DS", GITHUB_REPO: "a/b" };
const chat = { id: 777, type: "private" };
const updates = [
  { update_id: 9001, message: { message_id: 1, date: 1790000000, text: "email the TA tomorrow", chat } },
  { update_id: 9002, message: { message_id: 2, date: 1790000060, chat, voice: { file_id: "VOICE-1", duration: 3 } } },
  { update_id: 9003, message: { message_id: 3, date: 1790000120, chat, caption: "with a caption", voice: { file_id: "VOICE-2" } } },
];
const rows = [];
const fakeFetch = async (url, init) => {
  if (url.endsWith("/query")) return new Response(JSON.stringify({ results: [] }), { status: 200 });
  if (url.endsWith("/pages")) { rows.push(JSON.parse(init.body)); return new Response("{}", { status: 200 }); }
  return new Response(null, { status: 204 });
};
const log = console.log; console.log = () => {};
for (const u of updates) {
  await handle(new Request("https://r/", { method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": "s" }, body: JSON.stringify(u) }), ENV, fakeFetch);
}
console.log = log;
console.log(JSON.stringify(rows.map((r) => r.properties)));
