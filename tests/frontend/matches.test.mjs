import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import { test } from "node:test";
import { Window } from "happy-dom";
import { mountView, unmountView } from "../../frontend/dist/admin/index.js";

test("blacklist uses native table controls and opens profiles through the host with list state intact", async (t) => {
  const window = new Window({
    url: "https://minishop.test/shop/admin/minishop-blacklist?blacklist_page=2&blacklist_q=Olivia#list",
  });
  const profile = {
    user_id: 30, minishop_id: "ms_fixture", telegram_id: 1234,
    first_name: "Olivia", last_name: "Example", username: "user", email: null,
    avatar_url: null, telegram_photo_url: null, registration_date: "2026-11-18T12:00:00Z",
    panel_status: "active", panel_status_expired_at: null, subscription_expires_at: null,
    subscription_plan: {
      kind: "tariff", tariff_key: "standard", tariff_names: { en: "Standard" },
      is_configured: true, is_enabled: true, is_active: true,
    },
    is_banned: false, premium_traffic: { state: "none" },
    payments_total_amount: 10.7, payments_count: 3, payments_currency: "RUB",
    invited_users_count: 2, user_balance_amount_minor: 456, partner_balance_amount_minor: 0,
  };
  const requests = [];
  const fetch = async (path) => {
    const url = new URL(path, window.location.origin);
    assert.equal(url.pathname, "/api/admin/minishop-blacklist/matches");
    requests.push(url.searchParams);
    return Response.json({
      ok: true, enabled: true, total: 50,
      user_balance_enabled: true, balance_currency: "RUB", balance_currency_scale: 2,
      tariff_options: [profile.subscription_plan],
      rows: [{
        userId: "ms_fixture", telegramId: "1234", name: "Olivia Example", username: "user", profile,
        state: "match", stale: false,
        reasons: [{ text: "Duplicate refund request", links: ["https://example.test/case"] }],
      }],
    });
  };

  const globals = { window, document: window.document, PopStateEvent: window.PopStateEvent, fetch };
  const original = Object.fromEntries(Object.keys(globals).map((key) => [key, globalThis[key]]));
  Object.assign(globalThis, globals);
  t.after(async () => {
    for (const [name, value] of Object.entries(original)) {
      if (value === undefined) delete globalThis[name];
      else globalThis[name] = value;
    }
    await window.close();
  });
  const target = window.document.createElement("div");
  const main = window.document.createElement("main");
  main.className = "admin-main";
  main.append(target);
  window.document.body.append(main);
  const open = [];
  const host = { currentLang: "en", routePrefix: "/shop", onOpenUserCard: (user) => open.push(user) };
  let instance = mountView("matched-users", target, host);
  await setImmediate();

  assert.equal(requests[0].get("q"), "Olivia");
  assert.equal(requests[0].get("offset"), "25");
  const headers = [...target.querySelectorAll("thead th")].map((header) => header.textContent.trim());
  assert.deepEqual(headers.slice(0, 3), ["User↕", "Reasons and links", "Tariff / trial"]);
  const reasonCell = target.querySelector("tbody tr").children[1];
  assert.match(reasonCell.textContent, /Duplicate refund request/u);
  assert.equal(reasonCell.querySelector("a").href, "https://example.test/case");
  assert.match(target.querySelector("table").textContent, /Tariff \/ trial.*Balance.*Premium traffic.*Paid total/u);
  assert.match(target.querySelector(".admin-user-mobile-card").textContent, /Standard.*Duplicate refund request/u);
  main.scrollTop = 180;
  const click = new window.MouseEvent("click", { bubbles: true, cancelable: true });
  target.querySelector("tbody .admin-user-cell-name").dispatchEvent(click);
  assert.equal(click.defaultPrevented, true);
  assert.equal(open[0].minishop_id, "ms_fixture");
  const originalListUrl = window.location.href;
  unmountView(instance);
  main.scrollTop = 0;

  instance = mountView("matched-users", target, host);
  await setImmediate();
  assert.equal(window.location.href, originalListUrl);
  assert.equal(main.scrollTop, 180);
  assert.equal(requests.at(-1).get("offset"), "25");
  assert.equal(target.querySelector('input[name="search"]').value, "Olivia");

  const tariffFilter = target.querySelector('select[name="tariff_filter"]');
  tariffFilter.value = "tariff:standard";
  tariffFilter.dispatchEvent(new window.Event("change", { bubbles: true }));
  await setImmediate();
  assert.equal(requests.at(-1).get("tariff_filter"), "tariff:standard");
  assert.equal(requests.at(-1).get("offset"), "0");
  target.querySelector('button[name="sort-payments_count"]').click();
  await setImmediate();
  assert.equal(requests.at(-1).get("sort"), "payments_count_desc");
  assert.equal(target.querySelector('button[name="sort-payments_count"]').closest("th").getAttribute("aria-sort"), "descending");

  const action = target.querySelector('[data-admin-action="open-relationships-graph"]');
  action.click();
  assert.equal(window.location.pathname, "/shop/admin/users");
  assert.equal(new URL(window.location.href).searchParams.get("view"), "relationships");
  unmountView(instance);
});
