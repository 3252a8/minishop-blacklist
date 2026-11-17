import { ApiError } from "./api";
import { clearDiagnostics, getDiagnostics, sendTestAlert, setDebugEnabled } from "./diagnostics-api";
import type { DiagnosticEntry, DiagnosticsData } from "./diagnostics-api";
import { button, element, errorText, language, root, stateMessage, translator } from "./ui";
import type { Props, Translate, View } from "./ui";
import "./diagnostics.css";

const reasons = [
  "notifications_disabled", "event_disabled", "no_sources", "invalid_event",
  "panel_sync", "telegram_unlinked", "telegram_changed", "no_match", "duplicate",
  "non_positive_payment", "destination_changed", "event_not_committed", "sources_pending",
  "telegram_unavailable", "generation_changed", "queue_failed", "telegram_api_error",
  "telegram_network_error", "telegram_timeout", "telegram_rate_limited", "telegram_unauthorized",
  "telegram_forbidden", "telegram_chat_not_found", "telegram_topic_not_found",
  "telegram_invalid_message", "telegram_invalid_button", "telegram_chat_migrated",
] as const;

function displayReason(value: string, t: Translate): string {
  if (value === "chat_required") return t(value);
  const code = reasons.find((reason) => value === reason);
  return code ? t(`debug_reason_${code}`) : value;
}

function logItem(entry: DiagnosticEntry, t: Translate, date: Intl.DateTimeFormat): HTMLElement {
  const item = element("li");
  const details = element("details");
  details.dataset.id = entry.id;
  const summary = element("summary");
  const timestamp = new Date(entry.timestamp);
  const time = element("time", Number.isNaN(timestamp.getTime()) ? entry.timestamp : date.format(timestamp));
  time.dateTime = entry.timestamp;
  const state = element("strong", t(`debug_status_${entry.status}`));
  state.dataset.status = entry.status;
  summary.append(time, element("span", t(entry.event === "test" ? "debug_test_event" : entry.event)), state);
  if (entry.reason) summary.append(element("span", displayReason(entry.reason, t), "blacklist-muted"));
  details.append(summary);

  const metadata = element("dl");
  for (const [key, value] of [
    ["debug_operation", entry.operationId], ["user_column", entry.userId], ["telegram_id", entry.telegramId],
  ] as const) {
    if (value) metadata.append(element("dt", t(key)), element("dd", value));
  }
  if (metadata.childNodes.length) details.append(metadata);
  if (entry.request) details.append(element("h3", t("debug_request")), element("pre", JSON.stringify(entry.request, null, 2)));
  if (entry.response) details.append(element("h3", t("debug_response")), element("pre", JSON.stringify(entry.response, null, 2)));
  item.append(details);
  return item;
}

export class DiagnosticsView implements View {
  private readonly node: HTMLElement;
  private readonly enabled = element("input");
  private readonly enabledLabel = element("span");
  private readonly hint = element("p", undefined, "blacklist-muted");
  private readonly destination = element("p", undefined, "blacklist-muted");
  private readonly notice = element("div");
  private readonly list = element("ol", undefined, "blacklist-log-list");
  private readonly refresh = button("", () => { void this.load(); });
  private readonly test = button("", () => { void this.perform(sendTestAlert, "debug_test_queued"); });
  private readonly clear = button("", () => {
    if (window.confirm(translator(this.props)("debug_clear_confirm"))) {
      void this.perform(clearDiagnostics, "debug_cleared");
    }
  });
  private readonly more = button("", () => { void this.load(this.data?.nextBeforeId ?? undefined); });
  private readonly ticker: ReturnType<typeof setInterval>;
  private operation: AbortController | null = null;
  private polling = false;
  private data: DiagnosticsData | null = null;
  private failure: { error: unknown } | null = null;
  private message: "debug_test_queued" | "debug_cleared" | null = null;
  private renderedEntries = "";
  private paged = false;

  constructor(target: HTMLElement, private props: Props) {
    this.node = root(target);
    this.node.classList.add("blacklist-diagnostics");
    this.enabled.type = "checkbox";
    this.enabled.name = "debugEnabled";
    this.enabled.addEventListener("change", () => {
      const enable = this.enabled.checked;
      void this.perform((signal) => setDebugEnabled(enable, signal));
    });
    const label = element("label", undefined, "blacklist-check");
    label.append(this.enabled, this.enabledLabel);
    const actions = element("div", undefined, "blacklist-log-actions");
    this.test.classList.add("blacklist-primary");
    actions.append(this.test, this.refresh, this.clear);
    this.node.append(label, this.hint, this.destination, actions, this.notice, this.list, this.more);
    this.ticker = setInterval(() => {
      if (!document.hidden && this.props.active !== false && this.data?.enabled && !this.paged) {
        void this.load(undefined, true);
      }
    }, 4000);
    void this.load();
  }

  update(props: Props): void {
    const changed = language(this.props) !== language(props);
    this.props = props;
    if (changed) this.render();
  }

  destroy(): void {
    clearInterval(this.ticker);
    this.operation?.abort();
    this.node.remove();
  }

  private load(beforeId?: string, quiet = false): Promise<void> {
    return this.perform(async (signal) => {
      const next = await getDiagnostics(signal, beforeId);
      if (signal.aborted) return;
      const seen = new Set(this.data?.entries.map((entry) => entry.id));
      this.data = beforeId && this.data
        ? { ...next, entries: [...this.data.entries, ...next.entries.filter(({ id }) => !seen.has(id))] }
        : next;
      this.paged = Boolean(beforeId);
    }, undefined, quiet, false);
  }

  private async perform(
    action: (signal: AbortSignal) => Promise<void>,
    message?: NonNullable<DiagnosticsView["message"]>,
    quiet = false,
    reload = true,
  ): Promise<void> {
    if (this.operation && (quiet || !this.polling)) return;
    this.operation?.abort();
    const controller = new AbortController();
    this.operation = controller;
    this.polling = quiet;
    if (!quiet) {
      this.failure = null;
      this.message = null;
      this.render();
    }
    try {
      await action(controller.signal);
      if (controller.signal.aborted) return;
      if (reload) {
        const next = await getDiagnostics(controller.signal);
        if (controller.signal.aborted) return;
        this.data = next;
        this.paged = false;
      }
      this.failure = null;
      if (message) this.message = message;
    } catch (error) {
      if (!controller.signal.aborted && !quiet) this.failure = { error };
    } finally {
      if (!controller.signal.aborted) {
        this.operation = null;
        this.render();
      }
    }
  }

  private render(): void {
    const t = translator(this.props);
    const busy = Boolean(this.operation);
    const data = this.data;
    this.enabledLabel.textContent = t("debug_enabled");
    this.enabled.checked = data?.enabled ?? false;
    this.enabled.disabled = busy || !data;
    this.hint.textContent = t("debug_hint");
    this.destination.textContent = data?.chatId
      ? t(data.topicId ? "debug_destination_topic" : "debug_destination", { chat: data.chatId, topic: data.topicId ?? "" })
      : t("debug_chat_unset");
    this.refresh.textContent = t("debug_refresh");
    this.refresh.disabled = busy;
    this.test.textContent = t("debug_send_test");
    this.test.disabled = busy || !data?.enabled || !data.chatId;
    this.clear.textContent = t("debug_clear");
    this.clear.disabled = busy || !data?.entries.length;
    this.more.textContent = t("debug_more");
    this.more.hidden = !data?.nextBeforeId;
    this.more.disabled = busy;
    this.notice.replaceChildren();
    if (this.failure) {
      const { error } = this.failure;
      const text = error instanceof ApiError && error.code === "debug_disabled"
        ? t("debug_enable_first") : errorText(t, error, "request_failed");
      this.notice.append(stateMessage(text, "alert"));
    } else if (!data) this.notice.append(stateMessage(t("loading")));
    else if (this.message) this.notice.append(stateMessage(t(this.message)));
    else if (!data.entries.length) this.notice.append(stateMessage(t(data.enabled ? "debug_empty" : "debug_empty_off")));

    const ids = data?.entries.map(({ id }) => id).join(",") ?? "";
    const locale = language(this.props);
    if (`${locale}:${ids}` === this.renderedEntries) return;
    this.renderedEntries = `${locale}:${ids}`;
    const expanded = new Set([...this.list.querySelectorAll("details[open]")].map((details) => details.getAttribute("data-id")));
    const date = new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "medium" });
    this.list.replaceChildren(...(data?.entries ?? []).map((entry) => {
      const item = logItem(entry, t, date);
      if (expanded.has(entry.id)) item.firstElementChild?.setAttribute("open", "");
      return item;
    }));
  }
}
