import { array, boolean, choice, nullable, record, request, string } from "./api";

const events = ["register", "trial", "payment", "expired", "test"] as const;
const states = ["queued", "skipped", "sending", "sent", "retry", "error"] as const;

export interface DiagnosticEntry {
  id: string;
  timestamp: string;
  event: (typeof events)[number];
  status: (typeof states)[number];
  reason: string | null;
  operationId: string | null;
  userId: string | null;
  telegramId: string | null;
  request: Record<string, unknown> | null;
  response: Record<string, unknown> | null;
}

export interface DiagnosticsData {
  enabled: boolean;
  chatId: string | null;
  topicId: string | null;
  entries: DiagnosticEntry[];
  nextBeforeId: string | null;
}

export async function getDiagnostics(signal: AbortSignal, beforeId?: string): Promise<DiagnosticsData> {
  const cursor = beforeId ? `&beforeId=${encodeURIComponent(beforeId)}` : "";
  const payload = await request(`/diagnostics?limit=30${cursor}`, signal);
  return {
    enabled: boolean(payload.enabled),
    chatId: nullable(payload.chatId, string),
    topicId: nullable(payload.topicId, string),
    nextBeforeId: nullable(payload.nextBeforeId, string),
    entries: array(payload.entries, (value) => {
      const entry = record(value);
      return {
        id: string(entry.id),
        timestamp: string(entry.timestamp),
        event: choice(entry.event, events),
        status: choice(entry.status, states),
        reason: nullable(entry.reason, string),
        operationId: nullable(entry.operationId, string),
        userId: nullable(entry.userId, string),
        telegramId: nullable(entry.telegramId, string),
        request: nullable(entry.request, record),
        response: nullable(entry.response, record),
      };
    }),
  };
}

export async function setDebugEnabled(enabled: boolean, signal: AbortSignal): Promise<void> {
  await request("/diagnostics", signal, "PUT", { enabled });
}

export async function sendTestAlert(signal: AbortSignal): Promise<void> {
  await request("/diagnostics/test", signal, "POST", {});
}

export async function clearDiagnostics(signal: AbortSignal): Promise<void> {
  await request("/diagnostics", signal, "DELETE", {});
}
