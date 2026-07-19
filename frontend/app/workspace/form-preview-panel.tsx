"use client";

import { useState } from "react";

import {
  draftFieldsFromPreview,
  FORM_FIELD_OPTIONS,
  previewField,
} from "./form-preview";
import type {
  Application,
  FormFieldKey,
  FormPreview,
  FormPreviewFieldSpec,
} from "./types";


export function FormPreviewPanel({
  application,
  busy,
  onCreate,
  preview,
}: {
  application: Application;
  busy: boolean;
  onCreate: (formReference: string, fields: FormPreviewFieldSpec[]) => Promise<void>;
  preview?: FormPreview;
}) {
  const [formReference, setFormReference] = useState(
    preview?.form_reference ?? "Application form copied for local review",
  );
  const [fields, setFields] = useState<FormPreviewFieldSpec[]>(
    () => draftFieldsFromPreview(preview),
  );
  const canRevise = ["ready", "form_previewed"].includes(application.status);

  function updateField(index: number, update: Partial<FormPreviewFieldSpec>) {
    const next = [...fields];
    next[index] = { ...next[index], ...update };
    setFields(next);
  }

  function addField() {
    let sequence = fields.length + 1;
    const existing = new Set(fields.map((field) => field.field_id));
    while (existing.has(`local-field-${sequence}`)) sequence += 1;
    setFields([...fields, previewField(sequence)]);
  }

  return (
    <section className="workspace-form-preview" aria-label="Local form-fill preview">
      <div className="workspace-form-preview-heading">
        <div>
          <span className="workspace-kicker">LOCAL PREVIEW ONLY</span>
          <h3>Plan fields from verified evidence</h3>
          <p>
            Describe fields here without a URL or selector. CareerPilot does not open,
            fill, upload to, or change an employer form in this phase.
          </p>
        </div>
        {preview ? (
          <span className={preview.all_required_mapped ? "is-ready" : "is-unresolved"}>
            {preview.all_required_mapped
              ? "Required fields mapped"
              : `${preview.unresolved_required_count} required unresolved`}
          </span>
        ) : null}
      </div>

      {preview ? (
        <div className="workspace-preview-results" aria-live="polite">
          {preview.fields.map((field) => (
            <article className={`workspace-preview-field is-${field.state}`} key={field.field_id}>
              <div>
                <strong>{field.label}</strong>
                <span>{field.field_key.replaceAll("_", " ")}</span>
              </div>
              <b>{field.state}</b>
              <p>{field.value ?? field.reason}</p>
              {field.value ? <small>{field.reason}</small> : null}
            </article>
          ))}
          <div className="workspace-preview-safety">
            <strong>No external action occurred.</strong>
            <span>
              Browser use, navigation, clicks, uploads, filling, and submission are all
              recorded as false. A later external phase is not implemented and would
              require a new explicit confirmation.
            </span>
          </div>
        </div>
      ) : null}

      {canRevise ? (
        <details className="workspace-preview-builder" open={!preview}>
          <summary>{preview ? "Revise the local field checklist" : "Describe the local field checklist"}</summary>
          <label>
            Form reference
            <input
              maxLength={300}
              onChange={(event) => setFormReference(event.target.value)}
              value={formReference}
            />
          </label>
          <div className="workspace-preview-draft-fields">
            {fields.map((field, index) => (
              <div key={field.field_id}>
                <label>
                  Local field label
                  <input
                    maxLength={300}
                    onChange={(event) => updateField(index, { label: event.target.value })}
                    value={field.label}
                  />
                </label>
                <label>
                  Evidence mapping
                  <select
                    onChange={(event) => updateField(index, {
                      field_key: event.target.value as FormFieldKey,
                      options: ["resume", "cover_letter"].includes(event.target.value)
                        ? []
                        : field.options,
                    })}
                    value={field.field_key}
                  >
                    {FORM_FIELD_OPTIONS.map((option) => (
                      <option key={option.key} value={option.key}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Exact choices, one per line (optional)
                  <textarea
                    disabled={["resume", "cover_letter"].includes(field.field_key)}
                    onChange={(event) => updateField(index, {
                      options: event.target.value.split("\n").map((value) => value.trim()).filter(Boolean),
                    })}
                    rows={2}
                    value={field.options.join("\n")}
                  />
                </label>
                <label className="workspace-preview-required">
                  <input
                    checked={field.required}
                    onChange={(event) => updateField(index, { required: event.target.checked })}
                    type="checkbox"
                  />
                  Required on the form
                </label>
                <button
                  disabled={fields.length === 1}
                  onClick={() => setFields(fields.filter((_, fieldIndex) => fieldIndex !== index))}
                  type="button"
                >Remove</button>
              </div>
            ))}
          </div>
          <div className="workspace-preview-builder-actions">
            <button disabled={fields.length >= 50} onClick={addField} type="button">Add field</button>
            <button
              disabled={busy || !formReference.trim() || fields.some((field) => !field.label.trim())}
              onClick={() => void onCreate(formReference.trim(), fields)}
              type="button"
            >{busy ? "Building preview…" : preview ? "Rebuild local preview" : "Build local preview"}</button>
          </div>
        </details>
      ) : null}
    </section>
  );
}
