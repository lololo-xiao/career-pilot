const KEYBOARD_OCCLUSION_THRESHOLD = 80;

export interface NativeViewportInput {
  hasFocusedTextControl: boolean;
  layoutHeight: number;
  visualHeight?: number;
  visualOffsetTop?: number;
}

export interface NativeViewportState {
  height: number;
  keyboardVisible: boolean;
  offsetTop: number;
}

function positiveFinite(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : fallback;
}

export function getNativeViewportState(input: NativeViewportInput): NativeViewportState {
  const layoutHeight = positiveFinite(input.layoutHeight, 1);
  const visualHeight = Math.min(
    layoutHeight,
    positiveFinite(input.visualHeight, layoutHeight),
  );
  const visualOffsetTop = Math.min(
    Math.max(0, layoutHeight - visualHeight),
    Math.max(
      0,
      Number.isFinite(input.visualOffsetTop) ? input.visualOffsetTop ?? 0 : 0,
    ),
  );
  const occludedHeight = Math.max(
    0,
    layoutHeight - visualHeight - visualOffsetTop,
  );

  return {
    height: Math.max(1, Math.round(visualHeight)),
    keyboardVisible:
      input.hasFocusedTextControl
      && occludedHeight >= KEYBOARD_OCCLUSION_THRESHOLD,
    offsetTop: Math.round(visualOffsetTop),
  };
}
