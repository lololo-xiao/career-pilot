export type ApprovalHistoryState =
  | "pending"
  | "approved"
  | "denied"
  | "expired"
  | "consumed";

export interface ApprovalHistoryContext {
  label: string;
  value: string;
}

export interface ApprovalHistoryBinding {
  actionType: string;
  algorithm: "sha256-canonical-json-v1";
  payloadSha256: string | null;
  maximumUses: 1;
  remainingUses: 0 | 1;
}

export interface ApprovalHistoryItem {
  id: string;
  state: ApprovalHistoryState;
  usable: boolean;
  requestedAt: string;
  lastTransitionAt: string | null;
  expiresAt: string;
  requestSummary: string | null;
  authorization: {
    title: string;
    effect: string;
    notAuthorized: string[];
    context: ApprovalHistoryContext[];
    binding: ApprovalHistoryBinding;
  };
}

export interface ApprovalHistoryPage {
  asOf: string;
  pendingCount: number;
  items: ApprovalHistoryItem[];
  nextCursor: string | null;
}

const STATES = new Set<ApprovalHistoryState>([
  "pending",
  "approved",
  "denied",
  "expired",
  "consumed",
]);
const SHA256 = /^[a-f0-9]{64}$/;
const TIMESTAMP_WITH_TIMEZONE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;
const CURSOR = /^[A-Za-z0-9_-]{1,512}$/;
const CURSOR_ID = /^[A-Za-z0-9_-]{1,64}$/;

function objectValue(value: unknown, message: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(message);
  return value as Record<string, unknown>;
}

function stringValue(value: unknown, maximum: number, message: string): string {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    throw new Error(message);
  }
  return value;
}

function nullableString(
  value: unknown,
  maximum: number,
  message: string,
): string | null {
  if (value === null) return null;
  return stringValue(value, maximum, message);
}

function dateValue(value: unknown): string {
  const date = stringValue(value, 40, "Approval history returned an invalid date.");
  if (!TIMESTAMP_WITH_TIMEZONE.test(date) || Number.isNaN(Date.parse(date))) {
    throw new Error("Approval history returned an invalid date.");
  }
  return date;
}

function historyCursor(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || !CURSOR.test(value)) {
    throw new Error("Approval history returned an invalid cursor.");
  }
  try {
    const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
    const decoded = JSON.parse(atob(padded.replaceAll("-", "+").replaceAll("_", "/"))) as unknown;
    const cursor = objectValue(decoded, "Approval history returned an invalid cursor.");
    if (
      Object.keys(cursor).sort().join(",") !== "i,t,v"
      || cursor.v !== 1
      || typeof cursor.i !== "string"
      || !CURSOR_ID.test(cursor.i)
      || typeof cursor.t !== "string"
    ) {
      throw new Error("Approval history returned an invalid cursor.");
    }
    dateValue(cursor.t);
  } catch {
    throw new Error("Approval history returned an invalid cursor.");
  }
  return value;
}

function parseItem(value: unknown): ApprovalHistoryItem {
  const item = objectValue(value, "Approval history returned an invalid record.");
  const state = item.state;
  if (typeof state !== "string" || !STATES.has(state as ApprovalHistoryState)) {
    throw new Error("Approval history returned an invalid state.");
  }
  if (typeof item.usable !== "boolean") {
    throw new Error("Approval history returned an invalid availability state.");
  }
  const authorization = objectValue(
    item.authorization,
    "Approval history returned an invalid authorization explanation.",
  );
  const binding = objectValue(
    authorization.binding,
    "Approval history returned an invalid request binding.",
  );
  const digest = binding.payload_sha256;
  if (digest !== null && (typeof digest !== "string" || !SHA256.test(digest))) {
    throw new Error("Approval history returned an invalid request fingerprint.");
  }
  if (
    binding.algorithm !== "sha256-canonical-json-v1"
    || binding.maximum_uses !== 1
    || ![0, 1].includes(binding.remaining_uses as number)
  ) {
    throw new Error("Approval history returned an invalid request binding.");
  }
  if (
    (item.usable === true && (state !== "approved" || binding.remaining_uses !== 1 || digest === null))
    || (item.usable === false && binding.remaining_uses !== 0)
  ) {
    throw new Error("Approval history returned an inconsistent request binding.");
  }
  if (!Array.isArray(authorization.not_authorized)
      || authorization.not_authorized.length < 1
      || authorization.not_authorized.length > 8) {
    throw new Error("Approval history returned invalid authorization limits.");
  }
  const notAuthorized = authorization.not_authorized.map((entry) => (
    stringValue(entry, 500, "Approval history returned invalid authorization limits.")
  ));
  if (!Array.isArray(authorization.context) || authorization.context.length > 4) {
    throw new Error("Approval history returned invalid request context.");
  }
  const context = authorization.context.map((entry): ApprovalHistoryContext => {
    const candidate = objectValue(entry, "Approval history returned invalid request context.");
    return {
      label: stringValue(candidate.label, 40, "Approval history returned invalid request context."),
      value: stringValue(candidate.value, 253, "Approval history returned invalid request context."),
    };
  });
  const lastTransitionAt = item.last_transition_at === null
    ? null
    : dateValue(item.last_transition_at);
  return {
    id: stringValue(item.id, 64, "Approval history returned an invalid record ID."),
    state: state as ApprovalHistoryState,
    usable: item.usable,
    requestedAt: dateValue(item.requested_at),
    lastTransitionAt,
    expiresAt: dateValue(item.expires_at),
    requestSummary: nullableString(
      item.request_summary,
      280,
      "Approval history returned an invalid request summary.",
    ),
    authorization: {
      title: stringValue(
        authorization.title,
        120,
        "Approval history returned an invalid authorization title.",
      ),
      effect: stringValue(
        authorization.effect,
        500,
        "Approval history returned an invalid authorization explanation.",
      ),
      notAuthorized,
      context,
      binding: {
        actionType: stringValue(
          binding.action_type,
          100,
          "Approval history returned an invalid action type.",
        ),
        algorithm: "sha256-canonical-json-v1",
        payloadSha256: digest as string | null,
        maximumUses: 1,
        remainingUses: binding.remaining_uses as 0 | 1,
      },
    },
  };
}

export function parseApprovalHistoryPage(value: unknown): ApprovalHistoryPage {
  const page = objectValue(value, "Approval history returned an invalid response.");
  if (page.schema_version !== "approval-history-v1") {
    throw new Error("Approval history returned an unsupported response version.");
  }
  if (!Number.isInteger(page.pending_count) || (page.pending_count as number) < 0) {
    throw new Error("Approval history returned an invalid pending count.");
  }
  if (!Array.isArray(page.items) || page.items.length > 50) {
    throw new Error("Approval history returned too many records.");
  }
  const nextCursor = historyCursor(page.next_cursor);
  return {
    asOf: dateValue(page.as_of),
    pendingCount: page.pending_count as number,
    items: page.items.map(parseItem),
    nextCursor,
  };
}

export function parseSessionAccountId(value: unknown): string | null {
  const response = objectValue(value, "CareerPilot returned an invalid local session.");
  if (response.authenticated !== true) return null;
  const user = objectValue(response.user, "CareerPilot returned an invalid local session.");
  return stringValue(user.id, 200, "CareerPilot returned an invalid local account.");
}

export function effectiveApprovalState(
  item: ApprovalHistoryItem,
  now = Date.now(),
): ApprovalHistoryState {
  if (
    (item.state === "pending" || item.state === "approved")
    && Date.parse(item.expiresAt) <= now
  ) {
    return "expired";
  }
  return item.state;
}

export function effectiveApprovalUsability(
  item: ApprovalHistoryItem,
  now = Date.now(),
): boolean {
  return item.usable && effectiveApprovalState(item, now) === "approved";
}

export function approvalScopeLabel(effectiveUsable: boolean): string {
  return effectiveUsable ? "Authorizes" : "Recorded scope";
}

export function approvalStateCopy(state: ApprovalHistoryState): string {
  return approvalStatePresentation(state, true).copy;
}

export interface ApprovalStatePresentation {
  label: string;
  copy: string;
  remainingUses: 0 | 1;
}

export function approvalStatePresentation(
  state: ApprovalHistoryState,
  usable: boolean,
): ApprovalStatePresentation {
  if (state === "pending") {
    return {
      label: "Waiting",
      copy: "Waiting for a decision; it cannot be used.",
      remainingUses: 0,
    };
  }
  if (state === "approved" && usable) {
    return {
      label: "Allowed once",
      copy: "Available for one exact matching action before expiry.",
      remainingUses: 1,
    };
  }
  if (state === "approved") {
    return {
      label: "Recorded only",
      copy: "Approved as a record, but no enabled CareerPilot consumer can use it.",
      remainingUses: 0,
    };
  }
  if (state === "consumed") {
    return {
      label: "Used",
      copy: "Used once; it cannot be reused.",
      remainingUses: 0,
    };
  }
  if (state === "denied") {
    return {
      label: "Denied",
      copy: "Denied; it authorizes no action.",
      remainingUses: 0,
    };
  }
  return {
    label: "Expired",
    copy: "Expired; it authorizes no action.",
    remainingUses: 0,
  };
}

export function mergeApprovalHistory(
  current: ApprovalHistoryItem[],
  incoming: ApprovalHistoryItem[],
  append: boolean,
): ApprovalHistoryItem[] {
  if (!append) return incoming;
  const ids = new Set(current.map((item) => item.id));
  return [...current, ...incoming.filter((item) => !ids.has(item.id))];
}

export interface ApprovalHistoryRequest {
  signal: AbortSignal;
  isCurrent: () => boolean;
}

export class ApprovalHistoryRequestCoordinator {
  private generation = 0;
  private accountId = "";
  private controller: AbortController | null = null;

  start(accountId: string): ApprovalHistoryRequest {
    this.controller?.abort();
    this.generation = this.generation >= Number.MAX_SAFE_INTEGER ? 1 : this.generation + 1;
    const generation = this.generation;
    this.accountId = accountId;
    const controller = new AbortController();
    this.controller = controller;
    return {
      signal: controller.signal,
      isCurrent: () => (
        !controller.signal.aborted
        && this.controller === controller
        && this.generation === generation
        && this.accountId === accountId
      ),
    };
  }

  invalidate(): void {
    this.controller?.abort();
    this.controller = null;
    this.accountId = "";
    this.generation = this.generation >= Number.MAX_SAFE_INTEGER ? 1 : this.generation + 1;
  }
}
