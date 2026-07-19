import type {
  FormFieldKey,
  FormPreview,
  FormPreviewFieldSpec,
} from "./types";


export const FORM_FIELD_OPTIONS: ReadonlyArray<{
  key: FormFieldKey;
  label: string;
}> = [
  { key: "full_name", label: "Full name" },
  { key: "email", label: "Email" },
  { key: "phone", label: "Phone" },
  { key: "location", label: "Location / address" },
  { key: "work_authorization", label: "Work authorization" },
  { key: "resume", label: "Resume / CV" },
  { key: "cover_letter", label: "Cover letter" },
  { key: "unsupported", label: "Unknown or unsupported field" },
];

export const DEFAULT_FORM_FIELDS: FormPreviewFieldSpec[] = [
  previewField(1, "Full name", "full_name"),
  previewField(2, "Email", "email"),
  previewField(3, "Phone", "phone"),
  previewField(4, "Resume", "resume"),
];

export function previewField(
  sequence: number,
  label = "Form field",
  fieldKey: FormFieldKey = "unsupported",
): FormPreviewFieldSpec {
  return {
    field_id: `local-field-${sequence}`,
    label,
    field_key: fieldKey,
    required: true,
    options: [],
  };
}

export function draftFieldsFromPreview(preview?: FormPreview): FormPreviewFieldSpec[] {
  if (!preview) return DEFAULT_FORM_FIELDS.map((field) => ({ ...field, options: [] }));
  return preview.fields.map((field) => ({
    field_id: field.field_id,
    label: field.label,
    field_key: field.field_key,
    required: field.required,
    options: [...field.options],
  }));
}

export function reconcileFormPreviews(
  current: Record<string, FormPreview>,
  loaded: Record<string, FormPreview>,
  authoritative = false,
): Record<string, FormPreview> {
  const reconciled = authoritative ? {} : { ...current };
  for (const [applicationId, preview] of Object.entries(loaded)) {
    if (!isSafeLocalPreview(preview, applicationId)) continue;
    reconciled[applicationId] = preview;
  }
  return reconciled;
}

function isSafeLocalPreview(preview: FormPreview, applicationId: string): boolean {
  const safety = preview?.safety;
  const validStates = new Set(["mapped", "unknown", "unsupported", "ambiguous"]);
  return preview?.application_id === applicationId
    && preview.schema_version === "local-form-fill-preview-v1"
    && preview.mode === "preview_only"
    && safety?.browser_used === false
    && safety.navigation_performed === false
    && safety.controls_clicked === false
    && safety.files_uploaded === false
    && safety.form_filled === false
    && safety.submitted === false
    && safety.external_mutation_performed === false
    && safety.later_external_phase_implemented === false
    && safety.later_explicit_confirmation_required === true
    && Array.isArray(preview.fields)
    && preview.fields.every((field) => validStates.has(field.state));
}
