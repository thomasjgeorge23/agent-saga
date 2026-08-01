"use client";

import React, { useState } from "react";

export interface FieldMeta {
  name: string;
  field_type: "text" | "number" | "boolean" | "json";
  required: boolean;
  default?: any;
  description?: string;
}

export interface UISchema {
  title: string;
  tool_name: string;
  semantics: "COMPENSABLE" | "REVERSIBLE" | "IRREVERSIBLE";
  fields: FieldMeta[];
}

export interface DeclarativeUICompilerProps {
  schema: UISchema;
  onSubmitSaga?: (formData: Record<string, any>) => Promise<void>;
}

export const DeclarativeUICompiler: React.FC<DeclarativeUICompilerProps> = ({ schema, onSubmitSaga }) => {
  const [formData, setFormData] = useState<Record<string, any>>(() => {
    const initial: Record<string, any> = {};
    schema.fields.forEach((f) => {
      initial[f.name] = f.default !== undefined ? f.default : f.field_type === "number" ? 0 : "";
    });
    return initial;
  });

  const [submitting, setSubmitting] = useState<boolean>(false);
  const [result, setResult] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setResult(null);
    try {
      if (onSubmitSaga) {
        await onSubmitSaga(formData);
      }
      setResult(`✓ Transaction [${schema.tool_name}] executed and WAL committed cleanly.`);
    } catch (err: any) {
      setResult(`⚠ Pre-Flight Violation / Rollback: ${err.message || String(err)}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={styles.card}>
      <header style={styles.header}>
        <div>
          <div style={styles.eyebrow}>SAGAOPS AUTO-UI COMPILER</div>
          <h3 style={styles.title}>{schema.title}</h3>
          <div style={styles.toolName}>
            Target Tool: <code>{schema.tool_name}</code>
          </div>
        </div>

        <span style={styles.semanticsTag}>{schema.semantics}</span>
      </header>

      <form onSubmit={handleSubmit} style={styles.form}>
        {schema.fields.map((field) => (
          <div key={field.name} style={styles.fieldGroup}>
            <label style={styles.label}>
              {field.name} {field.required && <span style={{ color: "#ef4444" }}>*</span>}
            </label>
            {field.description && <span style={styles.desc}>{field.description}</span>}

            {field.field_type === "boolean" ? (
              <input
                type="checkbox"
                checked={!!formData[field.name]}
                onChange={(e) => setFormData({ ...formData, [field.name]: e.target.checked })}
                style={styles.checkbox}
              />
            ) : (
              <input
                type={field.field_type === "number" ? "number" : "text"}
                value={formData[field.name]}
                onChange={(e) =>
                  setFormData({
                    ...formData,
                    [field.name]: field.field_type === "number" ? Number(e.target.value) : e.target.value,
                  })
                }
                style={styles.input}
                required={field.required}
              />
            )}
          </div>
        ))}

        <button type="submit" style={styles.submitBtn} disabled={submitting}>
          {submitting ? "⚡ Executing Transaction Boundary..." : `Execute ${schema.tool_name} →`}
        </button>
      </form>

      {result && <div style={styles.resultBox}>{result}</div>}
    </div>
  );
};

const styles: { [key: string]: React.CSSProperties } = {
  card: {
    background: "#0b1329",
    border: "1px solid #1e293b",
    borderRadius: "16px",
    padding: "1.8rem",
    color: "#f8fafc",
    fontFamily: 'system-ui, -apple-system, sans-serif',
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    borderBottom: "1px solid #1e293b",
    paddingBottom: "1rem",
    marginBottom: "1.5rem",
  },
  eyebrow: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    color: "#38bdf8",
    fontWeight: 700,
  },
  title: {
    fontSize: "1.4rem",
    fontWeight: 800,
    margin: "0.2rem 0",
  },
  toolName: {
    fontSize: "0.85rem",
    color: "#94a3b8",
  },
  semanticsTag: {
    fontFamily: "monospace",
    fontSize: "0.75rem",
    background: "rgba(168, 85, 247, 0.15)",
    color: "#a855f7",
    border: "1px solid rgba(168, 85, 247, 0.3)",
    padding: "0.35rem 0.75rem",
    borderRadius: "999px",
    fontWeight: 700,
  },
  form: {
    display: "flex",
    flexDirection: "column",
    gap: "1.2rem",
  },
  fieldGroup: {
    display: "flex",
    flexDirection: "column",
    gap: "0.3rem",
  },
  label: {
    fontSize: "0.9rem",
    fontWeight: 700,
  },
  desc: {
    fontSize: "0.78rem",
    color: "#64748b",
  },
  input: {
    background: "#050810",
    border: "1px solid #1e293b",
    borderRadius: "8px",
    padding: "0.65rem 0.9rem",
    color: "#f8fafc",
    fontSize: "0.9rem",
    outline: "none",
  },
  checkbox: {
    width: "20px",
    height: "20px",
    cursor: "pointer",
  },
  submitBtn: {
    background: "linear-gradient(135deg, #38bdf8, #0284c7)",
    color: "#070c16",
    border: "none",
    padding: "0.85rem",
    borderRadius: "10px",
    fontWeight: 800,
    cursor: "pointer",
    fontSize: "0.95rem",
    marginTop: "0.5rem",
  },
  resultBox: {
    marginTop: "1.2rem",
    padding: "0.9rem 1.1rem",
    background: "#050810",
    border: "1px solid #10b981",
    borderRadius: "10px",
    fontFamily: "monospace",
    fontSize: "0.85rem",
    color: "#10b981",
  },
};

export default DeclarativeUICompiler;
