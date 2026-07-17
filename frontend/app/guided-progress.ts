export type GuidedStepId =
  | "ai-connection"
  | "reviewed-profile"
  | "target-opportunity"
  | "grounded-fit";

export interface GuidedProgressInput {
  aiConnected: boolean;
  verifiedProfileFacts: number;
  targetOpportunities: number;
  hasGroundedFit: boolean;
}

export interface GuidedProgressStep {
  id: GuidedStepId;
  label: string;
  complete: boolean;
  current: boolean;
}

const STEP_LABELS: Record<GuidedStepId, string> = {
  "ai-connection": "Connect your AI",
  "reviewed-profile": "Review your profile",
  "target-opportunity": "Choose a target opportunity",
  "grounded-fit": "Run your first grounded fit",
};

export function getGuidedProgress(input: GuidedProgressInput): {
  completedCount: number;
  nextStep: GuidedStepId | null;
  steps: GuidedProgressStep[];
} {
  const completion: Record<GuidedStepId, boolean> = {
    "ai-connection": input.aiConnected,
    "reviewed-profile": input.verifiedProfileFacts > 0,
    "target-opportunity": input.targetOpportunities > 0,
    "grounded-fit": input.hasGroundedFit,
  };
  const ids = Object.keys(STEP_LABELS) as GuidedStepId[];
  const nextStep = ids.find((id) => !completion[id]) ?? null;

  return {
    completedCount: ids.filter((id) => completion[id]).length,
    nextStep,
    steps: ids.map((id) => ({
      id,
      label: STEP_LABELS[id],
      complete: completion[id],
      current: id === nextStep,
    })),
  };
}
