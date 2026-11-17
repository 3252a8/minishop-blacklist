export const EVENTS = ["register", "trial", "payment", "expired"] as const;
export const AREAS = ["users", "userCard", "ticket"] as const;

export type AlertEvent = (typeof EVENTS)[number];
export type DisplayArea = (typeof AREAS)[number];
export type DetailArea = Exclude<DisplayArea, "users">;

export interface Settings {
  sources: string[];
  refreshHours: number;
  display: Record<DisplayArea, boolean>;
  chatId: string | null;
  topicId: string | null;
  events: AlertEvent[];
}

interface SourceStatus {
  url: string;
  lastSuccessAt: string | null;
  lastAttemptAt: string | null;
  lastError: string | null;
  count: number | null;
  invalidLines: number | null;
}

export interface SettingsData {
  settings: Settings;
  sourceStatuses: SourceStatus[];
}

export interface Reason {
  text: string;
  links: string[];
}

export interface Status {
  state: "match" | "clear" | "unknown";
  reasons: Reason[];
  stale: boolean;
}

export interface MatchRow extends Status {
  userId: string;
  telegramId: string;
  name: string | null;
  username: string | null;
}

export interface MatchPage {
  enabled: boolean;
  rows: MatchRow[];
  total: number;
}

export class ApiError extends Error {
  constructor(readonly code: string, readonly status?: number) {
    super(code);
  }
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ApiError("invalid_response");
  return value as Record<string, unknown>;
}

function string(value: unknown): string {
  if (typeof value !== "string") throw new ApiError("invalid_response");
  return value;
}

function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new ApiError("invalid_response");
  return value;
}

function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) throw new ApiError("invalid_response");
  return value;
}

function nullable<T>(value: unknown, decode: (value: unknown) => T): T | null {
  return value === null ? null : decode(value);
}

function array<T>(value: unknown, decode: (value: unknown) => T): T[] {
  if (!Array.isArray(value)) throw new ApiError("invalid_response");
  return value.map(decode);
}

function choice<T extends string>(value: unknown, choices: readonly T[]): T {
  const selected = choices.find((item) => item === value);
  if (selected === undefined) throw new ApiError("invalid_response");
  return selected;
}

function decodeSettingsData(payload: Record<string, unknown>): SettingsData {
  const settings = record(payload.settings);
  const display = record(settings.display);
  return {
    settings: {
      sources: array(settings.sources, string),
      refreshHours: integer(settings.refreshHours),
      display: {
        users: boolean(display.users),
        userCard: boolean(display.userCard),
        ticket: boolean(display.ticket),
      },
      chatId: nullable(settings.chatId, string),
      topicId: nullable(settings.topicId, string),
      events: array(settings.events, (value) => choice(value, EVENTS)),
    },
    sourceStatuses: array(payload.sourceStatuses, (value) => {
      const source = record(value);
      return {
        url: string(source.url),
        lastSuccessAt: nullable(source.lastSuccessAt, string),
        lastAttemptAt: nullable(source.lastAttemptAt, string),
        lastError: nullable(source.lastError, string),
        count: nullable(source.count, integer),
        invalidLines: nullable(source.invalidLines, integer),
      };
    }),
  };
}

function decodeStatus(value: unknown): Status {
  const status = record(value);
  return {
    state: choice(status.state, ["match", "clear", "unknown"]),
    reasons: array(status.reasons, (value) => {
      const reason = record(value);
      return { text: string(reason.text), links: array(reason.links, string) };
    }),
    stale: boolean(status.stale),
  };
}

async function request(path: string, signal: AbortSignal, method = "GET", body?: object) {
  const headers: Record<string, string> = { Accept: "application/json" };
  const options: RequestInit = { method, headers, credentials: "same-origin", signal };
  if (body !== undefined) {
    const csrfCookie = document.cookie.match(/(?:^|;\s*)rw_webapp_csrf=([^;]*)/)?.[1];
    headers["Content-Type"] = "application/json";
    if (csrfCookie) headers["X-CSRF-Token"] = decodeURIComponent(csrfCookie);
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`/api/admin/minishop-blacklist${path}`, options);
  const data: unknown = await response.json().catch(() => null);
  const payload = data === null ? {} : record(data);
  if (!response.ok || payload.ok !== true) {
    throw new ApiError(typeof payload.error === "string" ? payload.error : "request_failed", response.status);
  }
  return payload;
}

export async function getSettings(signal: AbortSignal): Promise<SettingsData> {
  return decodeSettingsData(await request("/settings", signal));
}

export async function putSettings(settings: Settings, signal: AbortSignal): Promise<SettingsData> {
  return decodeSettingsData(await request("/settings", signal, "PUT", settings));
}

export async function getMatches(limit: number, offset: number, signal: AbortSignal): Promise<MatchPage> {
  const payload = await request(`/matches?limit=${limit}&offset=${offset}`, signal);
  return {
    enabled: boolean(payload.enabled),
    rows: array(payload.rows, (value) => {
      const row = record(value);
      return {
        ...decodeStatus(row),
        userId: string(row.userId),
        telegramId: string(row.telegramId),
        name: nullable(row.name, string),
        username: nullable(row.username, string),
      };
    }),
    total: integer(payload.total),
  };
}

export async function getStatus(view: DetailArea, id: string | null, signal: AbortSignal) {
  const payload = await request("/status", signal, "POST", { view, telegramIds: id ? [id] : [] });
  const statuses = record(payload.statuses);
  const enabled = boolean(payload.enabled);
  return { enabled, status: enabled && id ? decodeStatus(statuses[id]) : null };
}
