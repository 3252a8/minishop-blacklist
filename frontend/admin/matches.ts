import { getMatches } from "./api";
import type { MatchPage, MatchRow } from "./api";
import { button, cross, element, errorState, language, reasonList, root, stateMessage, translator } from "./ui";
import type { Props, Translate, View } from "./ui";

const PAGE_SIZE = 25;

function userLink(row: MatchRow, routePrefix: string): HTMLAnchorElement {
  const username = row.username ? `@${row.username}` : null;
  const link = element("a", row.name || username || row.userId);
  link.href = `${routePrefix.replace(/\/+$/u, "")}/admin/users/${encodeURIComponent(row.userId)}`;
  return link;
}

function matchesTable(rows: MatchRow[], routePrefix: string, t: Translate): HTMLElement {
  const wrapper = element("div", undefined, "blacklist-results");
  const table = element("table");
  table.setAttribute("aria-label", t("title"));
  const thead = element("thead");
  const header = element("tr");
  for (const name of [t("user_column"), t("telegram_id"), t("reason_column")]) {
    const cell = element("th", name);
    cell.scope = "col";
    header.append(cell);
  }
  thead.append(header);
  const tbody = element("tbody");
  for (const row of rows) {
    const tr = element("tr");
    const user = element("td");
    const identity = element("div", undefined, "blacklist-identity");
    identity.append(cross(t("match")), userLink(row, routePrefix));
    user.append(identity);
    const telegram = element("td", row.telegramId, "blacklist-id");
    const reasons = element("td");
    const comments = reasonList(row.reasons, t);
    if (comments.childNodes.length) reasons.append(comments);
    if (row.stale) reasons.append(element("p", t("stale"), "blacklist-muted"));
    tr.append(user, telegram, reasons);
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrapper.append(table);
  return wrapper;
}

export class MatchesView implements View {
  private readonly node: HTMLElement;
  private readonly controller = new AbortController();
  private page: MatchPage | null = null;
  private offset = 0;
  private loading = false;
  private failed: { offset: number; error: unknown } | null = null;

  constructor(target: HTMLElement, private props: Props) {
    this.node = root(target);
    void this.load(0);
  }

  update(props: Props): void {
    const changed = language(props) !== language(this.props) || props.routePrefix !== this.props.routePrefix;
    this.props = props;
    if (changed) this.render();
  }

  destroy(): void {
    this.controller.abort();
    this.node.remove();
  }

  private async load(offset: number): Promise<void> {
    if (this.loading || this.controller.signal.aborted) return;
    this.loading = true;
    this.failed = null;
    const focusedButton = document.activeElement instanceof HTMLButtonElement
      && this.node.contains(document.activeElement) ? document.activeElement.name : "";
    this.render();
    try {
      let page = await getMatches(PAGE_SIZE, offset, this.controller.signal);
      if (page.enabled && offset > 0 && offset >= page.total) {
        offset = Math.max(0, Math.floor((page.total - 1) / PAGE_SIZE) * PAGE_SIZE);
        page = await getMatches(PAGE_SIZE, offset, this.controller.signal);
      }
      this.offset = offset;
      this.page = page;
    } catch (error) {
      this.failed = { offset, error };
    } finally {
      this.loading = false;
      if (!this.controller.signal.aborted) {
        this.render();
        if (focusedButton && document.activeElement === document.body) {
          const buttons = this.node.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
          (Array.from(buttons).find((button) => button.name === focusedButton) ?? buttons[0])?.focus();
        }
      }
    }
  }

  private render(): void {
    const t = translator(this.props);
    this.node.replaceChildren();
    this.node.setAttribute("aria-busy", String(this.loading));
    if (this.failed) {
      const { error, offset } = this.failed;
      this.node.append(errorState(t, error, () => { void this.load(offset); }));
    }
    if (!this.page) {
      if (this.loading) this.node.append(stateMessage(t("loading")));
      return;
    }
    if (!this.page.enabled) {
      this.node.append(stateMessage(t("users_disabled")));
      return;
    }
    if (!this.page.rows.length) {
      this.node.append(stateMessage(t("users_empty")));
      return;
    }

    const { rows, total } = this.page;
    this.node.append(matchesTable(rows, this.props.routePrefix ?? "", t));
    const footer = element("div", undefined, "blacklist-toolbar");
    footer.append(stateMessage(t("count_range", { start: this.offset + 1, end: this.offset + rows.length, total })));
    if (total > PAGE_SIZE) {
      const controls = element("div", undefined, "blacklist-pagination");
      const previous = button(t("previous"), () => { void this.load(this.offset - PAGE_SIZE); });
      previous.name = "previous";
      previous.disabled = this.loading || this.offset === 0;
      const next = button(t("next"), () => { void this.load(this.offset + PAGE_SIZE); });
      next.name = "next";
      next.disabled = this.loading || this.offset + rows.length >= total;
      controls.append(previous, next);
      footer.append(controls);
    }
    this.node.append(footer);
  }
}
