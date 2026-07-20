import assert from "node:assert/strict";
import test from "node:test";

import { getNativeViewportState } from "../app/native-viewport.ts";


test("uses the visual viewport height while the iOS keyboard is visible", () => {
  assert.deepEqual(
    getNativeViewportState({
      hasFocusedTextControl: true,
      layoutHeight: 844,
      visualHeight: 503.4,
      visualOffsetTop: 0,
    }),
    { height: 503, keyboardVisible: true, offsetTop: 0 },
  );
});

test("does not infer a keyboard from a resize without a focused text control", () => {
  assert.deepEqual(
    getNativeViewportState({
      hasFocusedTextControl: false,
      layoutHeight: 844,
      visualHeight: 503,
      visualOffsetTop: 0,
    }),
    { height: 503, keyboardVisible: false, offsetTop: 0 },
  );
});

test("ignores minor viewport changes and falls back safely without VisualViewport", () => {
  assert.deepEqual(
    getNativeViewportState({
      hasFocusedTextControl: true,
      layoutHeight: 844,
      visualHeight: 800,
      visualOffsetTop: 8,
    }),
    { height: 800, keyboardVisible: false, offsetTop: 8 },
  );
  assert.deepEqual(
    getNativeViewportState({
      hasFocusedTextControl: true,
      layoutHeight: 844,
    }),
    { height: 844, keyboardVisible: false, offsetTop: 0 },
  );
});

test("aligns a panned visual viewport without extending beyond the layout", () => {
  assert.deepEqual(
    getNativeViewportState({
      hasFocusedTextControl: true,
      layoutHeight: 844,
      visualHeight: 800,
      visualOffsetTop: 44,
    }),
    { height: 800, keyboardVisible: false, offsetTop: 44 },
  );
});
