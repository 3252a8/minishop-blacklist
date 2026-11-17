import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import { test } from "node:test";
import { Window } from "happy-dom";
import { mountView, unmountView } from "../../frontend/dist/admin/index.js";

test("the Telegram log enables diagnostics, queues a test and renders the payload as text", async (t) => {
  const window = new Window({ url: "https://minishop.test/admin/plugins" });
  const original = { document: globalThis.document, fetch: globalThis.fetch };
  const document = window.document;
  document.documentElement.lang = "ru";
  document.cookie = "rw_webapp_csrf=fixture; path=/";
  let enabled = false;
  let requested = false;
  globalThis.document = document;
  globalThis.fetch = async (path, options) => {
    if (options.method === "PUT") {
      assert.equal(path, "/api/admin/minishop-blacklist/diagnostics");
      assert.equal(options.headers["X-CSRF-Token"], "fixture");
      enabled = JSON.parse(options.body).enabled;
      return Response.json({ ok: true, enabled });
    }
    if (options.method === "POST") {
      assert.equal(path, "/api/admin/minishop-blacklist/diagnostics/test");
      assert.equal(options.headers["X-CSRF-Token"], "fixture");
      requested = true;
      return Response.json({ ok: true, operationId: "test-job" });
    }
    assert.equal(path, "/api/admin/minishop-blacklist/diagnostics?limit=30");
    return Response.json({ ok: true, enabled, chatId: "-100123", topicId: "8", nextBeforeId: null,
      entries: requested ? [{
        id: "1", timestamp: "2026-11-18T08:00:00+00:00", event: "test", status: "error",
        reason: "telegram_topic_not_found", userId: null, telegramId: null, operationId: "test-job",
        request: { chat_id: -100123, message_thread_id: 8, text: "<b>Тест</b>", parse_mode: "HTML" },
        response: { detail: "Bad Request: message thread not found" },
      }] : [],
    });
  };

  const target = document.createElement("div");
  document.body.append(target);
  const view = mountView("diagnostics", target, {});
  t.after(async () => {
    unmountView(view);
    Object.assign(globalThis, original);
    await window.close();
  });
  await setImmediate();
  const toggle = target.querySelector('[name="debugEnabled"]');
  const testButton = [...target.querySelectorAll("button")].find((button) => /тестовое/u.test(button.textContent));
  assert.equal(testButton.disabled, true);
  toggle.click();
  await setImmediate();
  assert.equal(toggle.checked, true);
  assert.equal(testButton.disabled, false);
  testButton.click();
  await setImmediate();
  assert.match(target.textContent, /Тестовое уведомление поставлено в очередь/u);
  assert.match(target.querySelector("summary").textContent, /Telegram не нашёл топик/u);
  assert.match(target.querySelector("pre").textContent, /<b>Тест<\/b>/u);
  assert.equal(target.querySelector("pre b"), null);
  assert.match(target.textContent, /message thread not found/u);
});
