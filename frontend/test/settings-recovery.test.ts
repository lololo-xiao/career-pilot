import assert from "node:assert/strict";
import test from "node:test";

import {
  createSettingsDraftHydration,
  createSettingsReadController,
  getSettingsNavigationIntent,
  initialRecoverableReadState,
  isAuthSessionResponse,
  nextReadGeneration,
  readValidatedSettingsResponse,
  recoverableReadReducer,
  SETTINGS_READ_SECTIONS,
  SETTINGS_STEPS,
  settingsNavigatorReducer,
  settingsReadEndpoint,
  SettingsReadError,
  shouldRedirectForSession,
  startRecoverableSettingsRead,
  type RecoverableReadState,
  type SettingsReadController,
} from "../app/settings-recovery.ts";


interface TestSnapshot {
  draft: string;
  serverVersion: number;
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isTestSnapshot(value: unknown): value is TestSnapshot {
  return typeof value === "object"
    && value !== null
    && "draft" in value
    && typeof value.draft === "string"
    && "serverVersion" in value
    && typeof value.serverVersion === "number";
}

function response(value: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(value), init);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

test("redirects only for a valid response that explicitly says unauthenticated", async () => {
  const unauthenticated = await readValidatedSettingsResponse(
    response({ authenticated: false, user: null }),
    isAuthSessionResponse,
    "Session could not load.",
  );

  assert.equal(shouldRedirectForSession(unauthenticated), true);
  await assert.rejects(
    () => readValidatedSettingsResponse(
      response({ authenticated: false }),
      isAuthSessionResponse,
      "Session could not load.",
    ),
    (error: unknown) => error instanceof SettingsReadError && error.kind === "malformed",
  );
  await assert.rejects(
    () => readValidatedSettingsResponse(
      response({ authenticated: true, user: null }),
      isAuthSessionResponse,
      "Session could not load.",
    ),
    (error: unknown) => error instanceof SettingsReadError && error.kind === "malformed",
  );
});

test("keeps non-2xx, invalid JSON, and malformed responses recoverable", async () => {
  for (const [readResponse, kind] of [
    [response({ detail: "Temporarily unavailable" }, { status: 503 }), "http"],
    [new Response("<html>gateway error</html>", { status: 200 }), "invalid-json"],
    [response({ authenticated: "yes", user: null }), "malformed"],
  ] as const) {
    await assert.rejects(
      () => readValidatedSettingsResponse(
        readResponse,
        isAuthSessionResponse,
        "Session could not load.",
      ),
      (error: unknown) => error instanceof SettingsReadError && error.kind === kind,
    );
  }
});

test("each production read controller starts and retries only its own endpoint", async () => {
  for (const section of SETTINGS_READ_SECTIONS) {
    const controllers = new Map(
      SETTINGS_READ_SECTIONS.map((candidate) => [
        candidate,
        createSettingsReadController(candidate),
      ]),
    );
    const controller = controllers.get(section);
    assert.ok(controller);
    const requested: string[] = [];

    function start(controllerForSection: SettingsReadController) {
      return startRecoverableSettingsRead({
        apiBaseUrl: "https://careerpilot.test",
        controller: controllerForSection,
        fallback: "Read failed.",
        fetcher: async (url) => {
          requested.push(url);
          return response("ready");
        },
        onFailure() {
          assert.fail("the deterministic read should not fail");
        },
        onStart() {},
        onSuccess(payload) {
          assert.equal(payload, "ready");
        },
        signal: new AbortController().signal,
        validate: isString,
      });
    }

    const initial = start(controller);
    await initial.completion;
    controller.invalidate(initial.ticket);
    const retry = start(controller);
    await retry.completion;

    assert.deepEqual(requested, [
      settingsReadEndpoint("https://careerpilot.test", section),
      settingsReadEndpoint("https://careerpilot.test", section),
    ]);
    for (const [candidate, candidateController] of controllers) {
      if (candidate === section) {
        assert.equal(retry.ticket.generation, 3);
      } else {
        assert.equal(candidateController.begin().generation, 1);
      }
    }
  }
});

test("loaded data and an edited draft survive failure and successful retry", async () => {
  const controller = createSettingsReadController("identity");
  const draftHydration = createSettingsDraftHydration();
  let readState: RecoverableReadState<TestSnapshot> =
    initialRecoverableReadState<TestSnapshot>();
  let loaded: TestSnapshot | null = null;
  let draft = "";

  function start(fetcher: () => Promise<Response>) {
    return startRecoverableSettingsRead({
      apiBaseUrl: "",
      controller,
      fallback: "Identity could not load.",
      fetcher,
      onFailure(error, generation) {
        readState = recoverableReadReducer(readState, {
          type: "failure",
          generation,
          error,
        });
      },
      onStart(generation) {
        readState = recoverableReadReducer(readState, { type: "start", generation });
      },
      onSuccess(payload, generation) {
        loaded = payload;
        draftHydration.hydrate(() => {
          draft = payload.draft;
        });
        readState = recoverableReadReducer(readState, {
          type: "success",
          generation,
          data: payload,
        });
      },
      signal: new AbortController().signal,
      validate: isTestSnapshot,
    });
  }

  const initial = start(async () => response({
    draft: "server draft",
    serverVersion: 1,
  }));
  await initial.completion;
  assert.equal(draft, "server draft");
  draft = "unsaved user edit";

  controller.invalidate(initial.ticket);
  const failedRetry = start(async () => {
    throw new TypeError("network unavailable");
  });
  await failedRetry.completion;
  assert.deepEqual(loaded, { draft: "server draft", serverVersion: 1 });
  assert.deepEqual(readState.data, { draft: "server draft", serverVersion: 1 });
  assert.equal(readState.status, "error");
  assert.equal(draft, "unsaved user edit");

  controller.invalidate(failedRetry.ticket);
  const successfulRetry = start(async () => response({
    draft: "new server draft",
    serverVersion: 2,
  }));
  await successfulRetry.completion;
  assert.deepEqual(loaded, { draft: "new server draft", serverVersion: 2 });
  assert.deepEqual(readState.data, { draft: "new server draft", serverVersion: 2 });
  assert.equal(readState.status, "ready");
  assert.equal(draft, "unsaved user edit");
  assert.equal(
    draftHydration.hydrate(() => {
      draft = "overwritten";
    }),
    false,
  );
  assert.equal(draft, "unsaved user edit");
});

test("stale completion cannot run component data or draft side effects", async () => {
  const controller = createSettingsReadController("resources");
  const slowResponse = deferred<Response>();
  const currentResponse = deferred<Response>();
  const applied: string[] = [];

  function start(fetchPromise: Promise<Response>) {
    return startRecoverableSettingsRead({
      apiBaseUrl: "",
      controller,
      fallback: "Resources could not load.",
      fetcher: () => fetchPromise,
      onFailure(message) {
        applied.push(`failure:${message}`);
      },
      onStart() {},
      onSuccess(payload) {
        applied.push(payload);
      },
      signal: new AbortController().signal,
      validate: isString,
    });
  }

  const stale = start(slowResponse.promise);
  const current = start(currentResponse.promise);
  currentResponse.resolve(response("current"));
  await current.completion;
  slowResponse.resolve(response("stale"));
  await stale.completion;

  assert.deepEqual(applied, ["current"]);
});

test("post-unmount completion cannot run component success or failure side effects", async () => {
  const controller = createSettingsReadController("mcp");
  const pendingResponse = deferred<Response>();
  const applied: string[] = [];
  const read = startRecoverableSettingsRead({
    apiBaseUrl: "",
    controller,
    fallback: "MCP could not load.",
    fetcher: () => pendingResponse.promise,
    onFailure(message) {
      applied.push(`failure:${message}`);
    },
    onStart() {},
    onSuccess(payload) {
      applied.push(payload);
    },
    signal: new AbortController().signal,
    validate: isString,
  });

  controller.invalidate(read.ticket);
  pendingResponse.resolve(response("too late"));
  await read.completion;
  assert.deepEqual(applied, []);
});

test("generation wrap accepts the current read and rejects its pre-wrap predecessor", () => {
  assert.equal(nextReadGeneration(Number.MAX_SAFE_INTEGER), 1);
  const controller = createSettingsReadController(
    "capabilities",
    Number.MAX_SAFE_INTEGER - 1,
  );
  const preWrap = controller.begin();
  const wrapped = controller.begin();
  const applied: string[] = [];

  assert.equal(preWrap.generation, Number.MAX_SAFE_INTEGER);
  assert.equal(wrapped.generation, 1);
  assert.equal(controller.commit(preWrap, () => applied.push("stale")), false);
  assert.equal(controller.commit(wrapped, () => applied.push("current")), true);
  assert.deepEqual(applied, ["current"]);

  let state = initialRecoverableReadState("pre-wrap data");
  state = recoverableReadReducer(state, {
    type: "start",
    generation: Number.MAX_SAFE_INTEGER,
  });
  state = recoverableReadReducer(state, { type: "start", generation: 1 });
  assert.equal(state.generation, 1);
  assert.equal(state.data, "pre-wrap data");
});

test("the five-step navigator changes presentation only and has one current step", () => {
  const initial = { currentStep: SETTINGS_STEPS[0].id };
  const next = settingsNavigatorReducer(initial, {
    type: "select",
    step: "memories-skills",
  });

  assert.deepEqual(next, { currentStep: "memories-skills" });
  assert.equal(SETTINGS_STEPS.length, 5);
  assert.equal(
    SETTINGS_STEPS.filter((step) => step.id === next.currentStep).length,
    1,
  );
});

test("roving keyboard intent keeps focus in the navigator without content scrolling", () => {
  assert.deepEqual(getSettingsNavigationIntent("roving"), {
    focusContent: false,
    scrollContent: false,
    updateHash: true,
  });
  assert.deepEqual(getSettingsNavigationIntent("activate"), {
    focusContent: true,
    scrollContent: true,
    updateHash: true,
  });
});
