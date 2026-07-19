import type { AgentAsset, AgentAssetKind } from "./types";


export const MAX_SKILL_IMPORT_BYTES = 64 * 1024;
export const MAX_CANONICAL_SKILL_NAME_LENGTH = 64;
export const MAX_SKILL_DESCRIPTION_LENGTH = 1024;
export const MAX_LEGACY_ASSET_NAME_LENGTH = 80;
export const SKILL_NAME_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
export const LEGACY_EXPORT_SKILL_NAME_PATTERN =
  /^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$/;
const MEMORY_NAME_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$/;

const BUILT_IN_SKILL_NAMES = new Set([
  "application-assistance",
  "evidence-first-tailoring",
  "job-discovery",
]);

export interface ParsedSkillMarkdown {
  name: string;
  description: string;
  title: string;
  body: string;
  content: string;
  frontmatterFields: readonly ["name", "description"];
}

export interface CuratedSkillTemplate extends ParsedSkillMarkdown {
  source: "curated-template";
}

export interface SkillPreviewDraft {
  mode: "preview-only";
  source: "curated-template" | "local-import";
  sourceLabel: string;
  templateName: string | null;
  name: string;
  description: string;
  title: string;
  content: string;
}

export interface SkillInstallReadiness {
  canInstall: boolean;
  error: string | null;
}

export interface SkillExport {
  filename: string;
  content: string;
  mimeType: "text/markdown;charset=utf-8";
}

export class SkillLibraryError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SkillLibraryError";
  }
}

export function nextSkillPreviewGeneration(current: number): number {
  return Number.isSafeInteger(current) && current >= 0 && current < Number.MAX_SAFE_INTEGER
    ? current + 1
    : 1;
}

export function isCurrentSkillPreviewGeneration(
  expected: number,
  current: number,
): boolean {
  return expected === current;
}

const TEMPLATE_CONTENTS = [
  `---
name: target-company-brief
description: Build a source-aware company brief before evaluating a target employer or role.
---

# Build a target-company brief

1. Confirm the company, role, decision, and available sources.
2. Separate sourced company facts, candidate-provided facts, reasonable questions, and unknowns.
3. Cite each company claim or label it unverified.
4. Map role needs only to career evidence the candidate supplied or verified.
5. Highlight risks, open questions, and the next local research step.

Never invent career history, skills, relationships, outcomes, metrics, quotes, or company facts.
Do not send messages, contact people, apply to roles, submit forms, update records, schedule events, or otherwise change an external system.
`,
  `---
name: interview-story-bank
description: Organize verified career examples into a reusable interview story bank when preparing evidence-backed answers.
---

# Build an interview story bank

1. Extract examples only from candidate-provided career evidence.
2. Organize each example into situation, task, action, result, and supported skills.
3. Preserve the candidate's role and distinguish individual work from team work.
4. Mark missing context, uncertain chronology, and unsupported outcomes as questions.
5. Group reusable stories by competency without forcing one story to prove every skill.

Never invent career history, skills, relationships, outcomes, metrics, quotes, or company facts.
Do not send messages, contact people, apply to roles, submit forms, update records, schedule events, or otherwise change an external system.
`,
  `---
name: weekly-search-retrospective
description: Review a user-provided week of job-search activity and identify evidence-based adjustments for the next week.
---

# Run a weekly search retrospective

1. Summarize only the activities, decisions, and outcomes the user provided.
2. Separate observed patterns from hypotheses and label small samples.
3. Compare planned effort with completed effort without assigning unstated motives.
4. Identify one practice to continue, one friction to investigate, and up to three bounded experiments.
5. Keep proposed next steps local until the user chooses and performs them.

Never invent career history, skills, relationships, outcomes, metrics, quotes, or company facts.
Do not send messages, contact people, apply to roles, submit forms, update records, schedule events, or otherwise change an external system.
`,
  `---
name: networking-draft-review
description: Review a user-provided networking draft for factual support, clarity, boundaries, and respectful tone before the user decides what to do.
---

# Review a networking draft

1. Identify the draft's audience, purpose, requested action, and supplied relationship context.
2. Flag unsupported claims, assumed familiarity, pressure, ambiguity, and missing placeholders.
3. Suggest concise edits that preserve the user's voice and stated facts.
4. Keep names, relationships, referrals, and shared history unknown unless the user supplied them.
5. Return review notes and a clearly labeled local revision for the user to inspect.

Never invent career history, skills, relationships, outcomes, metrics, quotes, or company facts.
Do not send messages, contact people, apply to roles, submit forms, update records, schedule events, or otherwise change an external system.
`,
] as const;

function parseSingleQuotedScalar(value: string): string {
  if (!value.endsWith("'")) {
    throw new SkillLibraryError("Skill YAML frontmatter contains an unclosed quoted value.");
  }
  const inner = value.slice(1, -1);
  let parsed = "";
  for (let index = 0; index < inner.length; index += 1) {
    if (inner[index] !== "'") {
      parsed += inner[index];
      continue;
    }
    if (inner[index + 1] !== "'") {
      throw new SkillLibraryError("Skill YAML frontmatter contains an invalid quoted value.");
    }
    parsed += "'";
    index += 1;
  }
  return parsed;
}

function parseFrontmatterScalar(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) {
    throw new SkillLibraryError("Skill YAML frontmatter values cannot be empty.");
  }
  if (trimmed.startsWith('"')) {
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (typeof parsed !== "string") throw new Error("not a string");
      return parsed;
    } catch {
      throw new SkillLibraryError("Skill YAML frontmatter contains an invalid quoted value.");
    }
  }
  if (trimmed.startsWith("'")) return parseSingleQuotedScalar(trimmed);
  if (
    /^[!&*{}\[\],>|%@`]/.test(trimmed)
    || /^(?:null|~|true|false|yes|no|on|off)$/i.test(trimmed)
    || /^[-+]?\d+(?:\.\d+)?$/.test(trimmed)
    || /(?:^|\s)#/.test(trimmed)
    || /:\s/.test(trimmed)
    || /["']$/.test(trimmed)
  ) {
    throw new SkillLibraryError("Skill YAML frontmatter must use plain or quoted string values.");
  }
  return trimmed;
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

export function normalizeSkillName(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/-{2,}/g, "-")
    .slice(0, MAX_CANONICAL_SKILL_NAME_LENGTH)
    .replace(/-+$/g, "");
}

export function agentAssetNameMaxLength(
  kind: AgentAssetKind,
  creating = true,
): number {
  return kind === "skill" && creating
    ? MAX_CANONICAL_SKILL_NAME_LENGTH
    : MAX_LEGACY_ASSET_NAME_LENGTH;
}

export function isValidNewAgentAssetName(kind: AgentAssetKind, name: string): boolean {
  return kind === "skill"
    ? SKILL_NAME_PATTERN.test(name) && normalizeSkillName(name) === name
    : MEMORY_NAME_PATTERN.test(name);
}

export function isValidAgentAssetDraftName(
  kind: AgentAssetKind,
  name: string,
  creating: boolean,
): boolean {
  if (kind === "skill" && !creating) {
    return LEGACY_EXPORT_SKILL_NAME_PATTERN.test(name);
  }
  return isValidNewAgentAssetName(kind, name);
}

export function parseSkillMarkdown(content: string): ParsedSkillMarkdown {
  if (!content || !content.trim()) {
    throw new SkillLibraryError("The selected skill file is empty.");
  }
  if (content.includes("\0")) {
    throw new SkillLibraryError("Skill files cannot contain NUL bytes.");
  }
  if (utf8ByteLength(content) > MAX_SKILL_IMPORT_BYTES) {
    throw new SkillLibraryError("Skill files cannot exceed 64 KiB.");
  }

  const normalizedLines = content.replace(/\r\n?/g, "\n").split("\n");
  if (normalizedLines[0] !== "---") {
    throw new SkillLibraryError("Skills must start with YAML frontmatter.");
  }
  const closingIndex = normalizedLines.indexOf("---", 1);
  if (closingIndex < 0) {
    throw new SkillLibraryError("Skill YAML frontmatter is not closed.");
  }

  const fields = new Map<string, string>();
  for (const line of normalizedLines.slice(1, closingIndex)) {
    if (!line || line.includes("\t")) {
      throw new SkillLibraryError("Skill YAML frontmatter must contain one field per line.");
    }
    const match = /^([A-Za-z][A-Za-z0-9_-]*):[ ]+(.+)$/.exec(line);
    if (!match) {
      throw new SkillLibraryError("Skill YAML frontmatter is malformed.");
    }
    const [, key, rawValue] = match;
    if (fields.has(key)) {
      throw new SkillLibraryError(`Skill YAML frontmatter repeats '${key}'.`);
    }
    fields.set(key, parseFrontmatterScalar(rawValue));
  }

  const keys = [...fields.keys()];
  const unexpected = keys.filter((key) => key !== "name" && key !== "description");
  if (unexpected.length > 0) {
    throw new SkillLibraryError(
      `Skill YAML frontmatter allows only name and description; remove '${unexpected[0]}'.`,
    );
  }
  if (keys.length !== 2 || !fields.has("name") || !fields.has("description")) {
    throw new SkillLibraryError("Skill YAML frontmatter requires exactly name and description.");
  }

  const name = fields.get("name") ?? "";
  if (!SKILL_NAME_PATTERN.test(name) || normalizeSkillName(name) !== name) {
    throw new SkillLibraryError(
      "Skill names must use at most 64 normalized lowercase letters, numbers, and single hyphens, with no leading or trailing hyphen.",
    );
  }
  const description = fields.get("description")?.trim() ?? "";
  if (!description) {
    throw new SkillLibraryError("Skill YAML frontmatter requires a non-empty description.");
  }
  if (description.includes("<") || description.includes(">")) {
    throw new SkillLibraryError("Skill descriptions cannot contain angle brackets (< or >).");
  }
  if ([...description].length > MAX_SKILL_DESCRIPTION_LENGTH) {
    throw new SkillLibraryError("Skill descriptions cannot exceed 1,024 characters.");
  }

  const body = normalizedLines.slice(closingIndex + 1).join("\n").trim();
  if (!body) {
    throw new SkillLibraryError("Skill files require an instruction body after frontmatter.");
  }
  const title = body
    .split("\n")
    .find((line) => line.startsWith("# "))
    ?.slice(2)
    .trim() || name.replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());

  return {
    name,
    description,
    title,
    body,
    content,
    frontmatterFields: ["name", "description"],
  };
}

function createCuratedTemplate(content: string): CuratedSkillTemplate {
  return { ...parseSkillMarkdown(content), source: "curated-template" };
}

export const CURATED_SKILL_TEMPLATES: readonly CuratedSkillTemplate[] =
  TEMPLATE_CONTENTS.map(createCuratedTemplate);

if (
  new Set(CURATED_SKILL_TEMPLATES.map((template) => template.name)).size
    !== CURATED_SKILL_TEMPLATES.length
  || CURATED_SKILL_TEMPLATES.some((template) => BUILT_IN_SKILL_NAMES.has(template.name))
) {
  throw new SkillLibraryError("Curated skill template names must be unique and distinct from built-ins.");
}

export function createTemplatePreview(template: CuratedSkillTemplate): SkillPreviewDraft {
  return {
    mode: "preview-only",
    source: "curated-template",
    sourceLabel: "CareerPilot starter library",
    templateName: template.name,
    name: template.name,
    description: template.description,
    title: template.title,
    content: template.content,
  };
}

export function decodeSkillImport(bytes: Uint8Array, sourceLabel: string): SkillPreviewDraft {
  if (bytes.byteLength === 0) {
    throw new SkillLibraryError("The selected skill file is empty.");
  }
  if (bytes.byteLength > MAX_SKILL_IMPORT_BYTES) {
    throw new SkillLibraryError("Skill files cannot exceed 64 KiB.");
  }
  if (bytes.includes(0)) {
    throw new SkillLibraryError("Skill files cannot contain NUL bytes.");
  }

  let content: string;
  try {
    content = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new SkillLibraryError("Skill files must be valid UTF-8.");
  }
  const parsed = parseSkillMarkdown(content);
  return {
    mode: "preview-only",
    source: "local-import",
    sourceLabel,
    templateName: null,
    name: parsed.name,
    description: parsed.description,
    title: parsed.title,
    content,
  };
}

export async function readSkillImportFile(
  file: Pick<File, "name" | "size" | "arrayBuffer">,
): Promise<SkillPreviewDraft> {
  if (!Number.isSafeInteger(file.size) || file.size <= 0) {
    throw new SkillLibraryError("Select one non-empty regular file.");
  }
  if (file.size > MAX_SKILL_IMPORT_BYTES) {
    throw new SkillLibraryError("Skill files cannot exceed 64 KiB.");
  }
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  if (bytes.byteLength !== file.size) {
    throw new SkillLibraryError("The selected file changed while it was being read.");
  }
  const basename = file.name.split(/[\\/]/).pop()?.trim() || "local file";
  return decodeSkillImport(bytes, `Imported file · ${basename}`);
}

export function renameSkillPreview(
  preview: SkillPreviewDraft,
  requestedName: string,
): SkillPreviewDraft {
  const name = normalizeSkillName(requestedName);
  if (!SKILL_NAME_PATTERN.test(name)) return { ...preview, name };
  const content = preview.content.replace(/^name:[^\r\n]*$/m, `name: ${name}`);
  const parsed = parseSkillMarkdown(content);
  return {
    ...preview,
    name: parsed.name,
    description: parsed.description,
    title: parsed.title,
    content,
  };
}

export function findSkillNameConflict(
  name: string,
  existingNames: Iterable<string>,
): string | null {
  const normalized = normalizeSkillName(name);
  for (const existingName of existingNames) {
    if (existingName.toLowerCase() === normalized) return existingName;
  }
  return null;
}

export function getSkillInstallReadiness(
  preview: SkillPreviewDraft,
  existingNames: Iterable<string>,
): SkillInstallReadiness {
  if (!SKILL_NAME_PATTERN.test(preview.name) || normalizeSkillName(preview.name) !== preview.name) {
    return {
      canInstall: false,
      error: "Choose a normalized skill name with at most 64 characters.",
    };
  }
  let parsed: ParsedSkillMarkdown;
  try {
    parsed = parseSkillMarkdown(preview.content);
  } catch (error) {
    return {
      canInstall: false,
      error: error instanceof Error ? error.message : "The skill preview is invalid.",
    };
  }
  if (parsed.name !== preview.name) {
    return { canInstall: false, error: "The proposed name must match the frontmatter name." };
  }
  const conflict = findSkillNameConflict(preview.name, existingNames);
  if (conflict) {
    return {
      canInstall: false,
      error: `A built-in or user-owned skill named '${conflict}' already exists. Choose another name.`,
    };
  }
  return { canInstall: true, error: null };
}

export function createSkillInstallRequest(
  preview: SkillPreviewDraft,
  existingNames: Iterable<string>,
): { name: string; content: string } {
  const readiness = getSkillInstallReadiness(preview, existingNames);
  if (!readiness.canInstall) {
    throw new SkillLibraryError(readiness.error ?? "The skill preview cannot be installed.");
  }
  return { name: preview.name, content: preview.content };
}

export function buildUserSkillExport(asset: AgentAsset): SkillExport {
  if (asset.kind !== "skill" || asset.built_in || !asset.editable) {
    throw new SkillLibraryError("Only selected user-owned skills can be exported.");
  }
  if (!LEGACY_EXPORT_SKILL_NAME_PATTERN.test(asset.name)) {
    throw new SkillLibraryError("The selected skill does not have a safe export name.");
  }
  return {
    filename: `${asset.name}-SKILL.md`,
    content: asset.content,
    mimeType: "text/markdown;charset=utf-8",
  };
}
