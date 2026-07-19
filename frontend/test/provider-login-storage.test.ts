import assert from "node:assert/strict";
import test from "node:test";

import {
  codexAttemptStorageKey,
  createCodexAttemptStorageLifecycle,
  type SessionStorageLike,
} from "../app/provider-login-storage.ts";
import type { CodexLoginStartResponse } from "../app/types.ts";


function attempt(
  attemptId: string,
  expiresAt = 2_000,
): CodexLoginStartResponse {
  return {
    attempt_id: attemptId,
    expires_at: expiresAt,
    user_code: `CODE-${attemptId}`,
    verification_url: "https://example.test/device",
  };
}

function fakeStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  const operations: string[] = [];
  const storage: SessionStorageLike = {
    getItem(key) {
      operations.push(`get:${key}`);
      return values.get(key) ?? null;
    },
    removeItem(key) {
      operations.push(`remove:${key}`);
      values.delete(key);
    },
    setItem(key, value) {
      operations.push(`set:${key}`);
      values.set(key, value);
    },
  };
  return { operations, storage, values };
}

test("does not read, write, remove, or invent a fallback key before account hydration", () => {
  const fake = fakeStorage({
    "careerpilot_codex_login_attempt:local": JSON.stringify(attempt("fallback")),
  });
  const lifecycle = createCodexAttemptStorageLifecycle(fake.storage, () => 1_000);

  assert.equal(codexAttemptStorageKey(null), null);
  assert.equal(codexAttemptStorageKey(""), null);
  assert.equal(lifecycle.persist(null, attempt("unbound")), false);
  assert.equal(lifecycle.bind(null), null);
  assert.deepEqual(fake.operations, []);
  assert.equal(
    fake.values.get("careerpilot_codex_login_attempt:local"),
    JSON.stringify(attempt("fallback")),
  );
});

test("restores a valid unexpired attempt from only the bound account key", () => {
  const accountKey = codexAttemptStorageKey("account-a");
  assert.ok(accountKey);
  const fallbackKey = "careerpilot_codex_login_attempt:local";
  const saved = attempt("saved-a");
  const fake = fakeStorage({
    [accountKey]: JSON.stringify(saved),
    [fallbackKey]: JSON.stringify(attempt("fallback")),
  });
  const lifecycle = createCodexAttemptStorageLifecycle(fake.storage, () => 1_000);
  const binding = lifecycle.bind("account-a");

  assert.deepEqual(binding?.attempt, saved);
  assert.deepEqual(fake.operations, [`get:${accountKey}`]);
  assert.equal(fake.values.has(fallbackKey), true);
  assert.equal(lifecycle.persist(binding, attempt("updated-a")), true);
  assert.deepEqual(
    JSON.parse(fake.values.get(accountKey) ?? "null"),
    attempt("updated-a"),
  );
});

test("removes expired or malformed entries only from their exact account keys", () => {
  const keyA = codexAttemptStorageKey("account-a");
  const keyB = codexAttemptStorageKey("account-b");
  assert.ok(keyA);
  assert.ok(keyB);
  const fake = fakeStorage({
    [keyA]: JSON.stringify(attempt("expired", 999)),
    [keyB]: JSON.stringify({ attempt_id: "malformed" }),
  });
  const lifecycle = createCodexAttemptStorageLifecycle(fake.storage, () => 1_000);

  assert.equal(lifecycle.bind("account-a")?.attempt, null);
  assert.equal(fake.values.has(keyA), false);
  assert.equal(fake.values.has(keyB), true);
  assert.equal(lifecycle.bind("account-b")?.attempt, null);
  assert.equal(fake.values.has(keyB), false);
  assert.deepEqual(fake.operations, [
    `get:${keyA}`,
    `remove:${keyA}`,
    `get:${keyB}`,
    `remove:${keyB}`,
  ]);
});

test("changing account neither copies attempts nor lets an old binding delete another key", () => {
  const keyA = codexAttemptStorageKey("account-a");
  const keyB = codexAttemptStorageKey("account-b");
  const fallbackKey = "careerpilot_codex_login_attempt:local";
  assert.ok(keyA);
  assert.ok(keyB);
  const savedA = JSON.stringify(attempt("saved-a"));
  const fake = fakeStorage({
    [keyA]: savedA,
    [fallbackKey]: JSON.stringify(attempt("fallback")),
  });
  const lifecycle = createCodexAttemptStorageLifecycle(fake.storage, () => 1_000);
  const bindingA = lifecycle.bind("account-a");
  const bindingB = lifecycle.bind("account-b");

  assert.deepEqual(bindingA?.attempt, attempt("saved-a"));
  assert.equal(bindingB?.attempt, null);
  assert.equal(lifecycle.persist(bindingA, null), false);
  assert.equal(fake.values.get(keyA), savedA);
  assert.equal(fake.values.has(keyB), false);
  assert.equal(fake.values.has(fallbackKey), true);

  assert.equal(lifecycle.persist(bindingB, attempt("saved-b")), true);
  assert.deepEqual(
    JSON.parse(fake.values.get(keyB) ?? "null"),
    attempt("saved-b"),
  );
  assert.equal(fake.values.get(keyA), savedA);
});
