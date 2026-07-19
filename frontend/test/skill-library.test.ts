import assert from "node:assert/strict";
import test from "node:test";

import {
  agentAssetNameMaxLength,
  buildUserSkillExport,
  createSkillInstallRequest,
  createTemplatePreview,
  CURATED_SKILL_TEMPLATES,
  decodeSkillImport,
  findSkillNameConflict,
  getSkillInstallReadiness,
  isCurrentSkillPreviewGeneration,
  isValidAgentAssetDraftName,
  isValidNewAgentAssetName,
  MAX_CANONICAL_SKILL_NAME_LENGTH,
  MAX_LEGACY_ASSET_NAME_LENGTH,
  MAX_SKILL_DESCRIPTION_LENGTH,
  MAX_SKILL_IMPORT_BYTES,
  nextSkillPreviewGeneration,
  normalizeSkillName,
  parseSkillMarkdown,
  readSkillImportFile,
  renameSkillPreview,
  SkillLibraryError,
} from "../app/skill-library.ts";
import type { AgentAsset } from "../app/types.ts";


const BUILT_INS = [
  "application-assistance",
  "evidence-first-tailoring",
  "job-discovery",
];

function skillContent(name = "local-skill", lineEnding = "\n"): string {
  return [
    "---",
    `name: ${name}`,
    "description: Review supplied evidence without inventing facts.",
    "---",
    "",
    "# Review supplied evidence",
    "",
    "1. Check the supplied facts.",
    "2. Mark unknowns as questions.",
    "",
    "Never invent career facts.",
    "Do not send, contact, apply, submit, or change an external system.",
    "",
  ].join(lineEnding);
}

function userSkill(overrides: Partial<AgentAsset> = {}): AgentAsset {
  return {
    kind: "skill",
    name: "local-skill",
    title: "Local skill",
    description: "hidden response metadata",
    content: skillContent(),
    path: "/private/workspace/agent/skills/local-skill/SKILL.md",
    built_in: false,
    editable: true,
    ...overrides,
  };
}

test("ships four concise, unique, evidence-safe templates distinct from built-ins", () => {
  assert.equal(CURATED_SKILL_TEMPLATES.length, 4);
  assert.deepEqual(
    CURATED_SKILL_TEMPLATES.map((template) => template.name),
    [
      "target-company-brief",
      "interview-story-bank",
      "weekly-search-retrospective",
      "networking-draft-review",
    ],
  );
  assert.equal(new Set(CURATED_SKILL_TEMPLATES.map((template) => template.name)).size, 4);

  for (const template of CURATED_SKILL_TEMPLATES) {
    const parsed = parseSkillMarkdown(template.content);
    assert.deepEqual(parsed.frontmatterFields, ["name", "description"]);
    assert.equal(parsed.name, template.name);
    assert.ok(parsed.description.length > 20);
    assert.ok(template.content.split("\n").length < 35, `${template.name} should stay concise`);
    assert.ok(new TextEncoder().encode(template.content).byteLength < 4 * 1024);
    assert.ok(!BUILT_INS.includes(template.name));
    assert.match(parsed.body, /Never invent career history, skills, relationships, outcomes, metrics, quotes, or company facts\./);
    assert.match(parsed.body, /Do not send messages, contact people, apply to roles, submit forms, update records, schedule events, or otherwise change an external system\./);

    const instructions = parsed.body
      .split("\n")
      .filter((line) => line && !line.startsWith("#"));
    for (const instruction of instructions) {
      assert.match(
        instruction,
        /^(?:\d+\. (?:Confirm|Separate|Cite|Map|Highlight|Extract|Organize|Preserve|Mark|Group|Summarize|Compare|Identify|Keep|Flag|Suggest|Return)|Never |Do not )/,
        `${template.name} instruction should be imperative: ${instruction}`,
      );
    }
  }
});

test("checks the import byte limit before reading or decoding a file", async () => {
  let reads = 0;
  const oversized = {
    name: "SKILL.md",
    size: MAX_SKILL_IMPORT_BYTES + 1,
    async arrayBuffer() {
      reads += 1;
      return new ArrayBuffer(0);
    },
  };

  await assert.rejects(() => readSkillImportFile(oversized), /64 KiB/);
  assert.equal(reads, 0);
  await assert.rejects(
    () => readSkillImportFile({ ...oversized, size: 0 }),
    /non-empty regular file/,
  );
  assert.equal(reads, 0);
});

test("ignores slower import completions after a newer preview selection", () => {
  let current = 0;
  const slowImport = nextSkillPreviewGeneration(current);
  current = slowImport;
  const laterTemplate = nextSkillPreviewGeneration(current);
  current = laterTemplate;

  assert.equal(isCurrentSkillPreviewGeneration(slowImport, current), false);
  assert.equal(isCurrentSkillPreviewGeneration(laterTemplate, current), true);
  assert.equal(nextSkillPreviewGeneration(Number.MAX_SAFE_INTEGER), 1);
});

test("uses fatal UTF-8 decoding and rejects NUL bytes before parsing", () => {
  assert.throws(
    () => decodeSkillImport(Uint8Array.from([0xc3, 0x28]), "invalid.md"),
    /valid UTF-8/,
  );
  const withNul = new TextEncoder().encode(skillContent().replace("facts", "facts\0"));
  assert.throws(() => decodeSkillImport(withNul, "nul.md"), /NUL bytes/);
  assert.throws(() => decodeSkillImport(new Uint8Array(), "empty.md"), /empty/);
});

test("strictly parses SKILL.md frontmatter and preserves imported content", () => {
  const crlfContent = skillContent("quoted-skill", "\r\n").replace(
    "description: Review supplied evidence without inventing facts.",
    "description: 'Review the candidate''s supplied evidence.'",
  );
  const preview = decodeSkillImport(new TextEncoder().encode(crlfContent), "Imported file · SKILL.md");

  assert.equal(preview.mode, "preview-only");
  assert.equal(preview.source, "local-import");
  assert.equal(preview.name, "quoted-skill");
  assert.equal(preview.description, "Review the candidate's supplied evidence.");
  assert.equal(preview.content, crlfContent);

  for (const [label, malformed] of [
    ["extra field", skillContent().replace("description:", "metadata: unsafe\ndescription:")],
    ["duplicate name", skillContent().replace("description:", "name: local-skill\ndescription:")],
    ["missing description", skillContent().replace(/description:.*\n/, "")],
    ["unclosed frontmatter", skillContent().replace("\n---\n\n#", "\n#")],
    ["collection value", skillContent().replace("description: Review supplied evidence without inventing facts.", "description: [unsafe]")],
    ["empty body", "---\nname: local-skill\ndescription: Review evidence.\n---\n"],
    ["nonnormal name", skillContent("Local Skill")],
    ["repeated hyphen", skillContent("local--skill")],
  ] as const) {
    assert.throws(
      () => parseSkillMarkdown(malformed),
      SkillLibraryError,
      `${label} should be rejected`,
    );
  }
});

test("normalizes proposed names and blocks built-in or user collisions before install", () => {
  assert.equal(normalizeSkillName("  Développeur / Search__Review  "), "developpeur-search-review");
  assert.equal(normalizeSkillName("A".repeat(100)).length, 64);
  assert.equal(findSkillNameConflict("Job Discovery", BUILT_INS), "job-discovery");

  const original = createTemplatePreview(CURATED_SKILL_TEMPLATES[0]);
  const collision = getSkillInstallReadiness(original, [...BUILT_INS, original.name]);
  assert.equal(original.mode, "preview-only");
  assert.equal(collision.canInstall, false);
  assert.match(collision.error ?? "", /already exists/);
  assert.equal(original.content, CURATED_SKILL_TEMPLATES[0].content);

  const renamed = renameSkillPreview(original, "Target Company Brief — Personal");
  assert.equal(renamed.name, "target-company-brief-personal");
  assert.match(renamed.content, /\nname: target-company-brief-personal\n/);
  assert.equal(getSkillInstallReadiness(renamed, BUILT_INS).canInstall, true);
});

test("accepts canonical 64-character names and rejects imported 65-character names", () => {
  const name64 = "n".repeat(MAX_CANONICAL_SKILL_NAME_LENGTH);
  const name65 = `${name64}n`;
  const imported = decodeSkillImport(
    new TextEncoder().encode(skillContent(name64)),
    "Imported file · SKILL.md",
  );

  assert.equal(imported.name, name64);
  assert.throws(
    () => decodeSkillImport(
      new TextEncoder().encode(skillContent(name65)),
      "Imported file · SKILL.md",
    ),
    /at most 64/,
  );

  const renamed = renameSkillPreview(imported, name65);
  assert.equal(renamed.name, name64);
  assert.match(renamed.content, new RegExp(`\\nname: ${name64}\\n`));
});

test("enforces canonical description length and angle-bracket boundaries", () => {
  const description1024 = "d".repeat(MAX_SKILL_DESCRIPTION_LENGTH);
  const description1025 = `${description1024}d`;
  const withDescription = (description: string) => skillContent().replace(
    "description: Review supplied evidence without inventing facts.",
    `description: ${description}`,
  );

  const decodeDescription = (description: string) => decodeSkillImport(
    new TextEncoder().encode(withDescription(description)),
    "Imported file · SKILL.md",
  );

  assert.equal(decodeDescription(description1024).description.length, 1024);
  assert.throws(() => decodeDescription(description1025), /1,024/);
  assert.throws(() => decodeDescription("Review <company> facts."), /angle brackets/);
  assert.throws(() => decodeDescription("Review company > facts."), /angle brackets/);
});

test("keeps new skill names canonical without changing the memory name boundary", () => {
  const skill64 = "s".repeat(MAX_CANONICAL_SKILL_NAME_LENGTH);
  const skill65 = `${skill64}s`;
  const memory80 = "m".repeat(MAX_LEGACY_ASSET_NAME_LENGTH);
  const memory81 = `${memory80}m`;

  assert.equal(agentAssetNameMaxLength("skill"), 64);
  assert.equal(agentAssetNameMaxLength("memory"), 80);
  assert.equal(isValidNewAgentAssetName("skill", skill64), true);
  assert.equal(isValidNewAgentAssetName("skill", skill65), false);
  assert.equal(isValidNewAgentAssetName("memory", memory80), true);
  assert.equal(isValidNewAgentAssetName("memory", memory81), false);
  assert.equal(isValidAgentAssetDraftName("skill", skill65, true), false);
});

test("keeps content edits save-valid for a selected legacy 80-character skill", () => {
  const legacy80 = "l".repeat(MAX_LEGACY_ASSET_NAME_LENGTH);
  const legacy81 = `${legacy80}l`;

  assert.equal(agentAssetNameMaxLength("skill", false), 80);
  assert.equal(isValidAgentAssetDraftName("skill", legacy80, false), true);
  assert.equal(isValidAgentAssetDraftName("skill", legacy81, false), false);
});

test("keeps preview creation separate from the explicit install request", () => {
  const preview = createTemplatePreview(CURATED_SKILL_TEMPLATES[1]);

  assert.deepEqual(Object.keys(preview).sort(), [
    "content",
    "description",
    "mode",
    "name",
    "source",
    "sourceLabel",
    "templateName",
    "title",
  ]);
  assert.equal(preview.mode, "preview-only");

  const request = createSkillInstallRequest(preview, BUILT_INS);
  assert.deepEqual(request, { name: preview.name, content: preview.content });
  assert.throws(
    () => createSkillInstallRequest(preview, [...BUILT_INS, preview.name]),
    /already exists/,
  );
  assert.equal(preview.mode, "preview-only");
  assert.equal(preview.content, CURATED_SKILL_TEMPLATES[1].content);
});

test("prevents duplicate imported names while preserving a renameable preview", () => {
  const preview = decodeSkillImport(
    new TextEncoder().encode(skillContent("weekly-search-retrospective")),
    "Imported file · SKILL.md",
  );
  const blocked = getSkillInstallReadiness(preview, ["weekly-search-retrospective"]);

  assert.equal(blocked.canInstall, false);
  assert.equal(preview.mode, "preview-only");
  assert.equal(preview.name, "weekly-search-retrospective");
  const renamed = renameSkillPreview(preview, "Weekly Search Retrospective 2");
  assert.equal(getSkillInstallReadiness(renamed, [preview.name]).canInstall, true);
});

test("exports only exact user-owned skill content with a deterministic safe filename", () => {
  const asset = userSkill();
  const exported = buildUserSkillExport(asset);

  assert.deepEqual(exported, {
    filename: "local-skill-SKILL.md",
    content: asset.content,
    mimeType: "text/markdown;charset=utf-8",
  });
  assert.doesNotMatch(JSON.stringify(exported), /private\/workspace|hidden response metadata/);
  const importedAgain = decodeSkillImport(
    new TextEncoder().encode(exported.content),
    `Imported file · ${exported.filename}`,
  );
  assert.equal(importedAgain.content, asset.content);
  assert.equal(importedAgain.name, asset.name);

  const legacyName = "l".repeat(MAX_LEGACY_ASSET_NAME_LENGTH);
  const legacyContent = skillContent(legacyName);
  assert.equal(isValidAgentAssetDraftName("skill", legacyName, true), false);
  assert.equal(isValidAgentAssetDraftName("skill", legacyName, false), true);
  assert.deepEqual(buildUserSkillExport(userSkill({
    name: legacyName,
    content: legacyContent,
  })), {
    filename: `${legacyName}-SKILL.md`,
    content: legacyContent,
    mimeType: "text/markdown;charset=utf-8",
  });
  assert.throws(
    () => buildUserSkillExport(userSkill({ name: `${legacyName}l` })),
    /safe export name/,
  );

  assert.throws(
    () => buildUserSkillExport(userSkill({ built_in: true, editable: false })),
    /user-owned/,
  );
  assert.throws(
    () => buildUserSkillExport(userSkill({ kind: "memory" })),
    /user-owned/,
  );
});
