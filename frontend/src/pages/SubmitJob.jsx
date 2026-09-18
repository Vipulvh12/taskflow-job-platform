import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { submitJob } from "../api/jobs";
import { JobPayloadFields } from "../components/JobPayloadFields";
import { DEFAULT_PRIORITY, JOB_TYPES, MAX_PRIORITY, MIN_PRIORITY } from "../job-types";

export function SubmitJob() {
  const navigate = useNavigate();
  const [type, setType] = useState("");
  const [priority, setPriority] = useState(DEFAULT_PRIORITY);
  const [values, setValues] = useState({});
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  function handleTypeChange(newType) {
    setType(newType);
    setValues({}); // switching type invalidates whatever was typed for the old one
  }

  function buildPayload() {
    // Send only the fields this type declares, and drop empty optionals.
    // The backend's payload models use extra="forbid", so a stray key from a
    // previously selected type would come back as a 422.
    const fields = JOB_TYPES[type]?.fields ?? [];
    const payload = {};
    for (const field of fields) {
      const value = values[field.name];
      if (value === undefined || value === "") {
        if (field.required) payload[field.name] = "";
        continue;
      }
      payload[field.name] = value;
    }
    return payload;
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const job = await submitJob({
        type,
        payload: buildPayload(),
        priority: Number(priority),
      });
      navigate(`/jobs/${job.job_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Submission failed.");
    } finally {
      setSubmitting(false);
    }
  }

  const selected = JOB_TYPES[type];

  return (
    <form onSubmit={handleSubmit} className="card">
      <h1>Submit a job</h1>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      <label>
        Job type
        <select value={type} onChange={(e) => handleTypeChange(e.target.value)} required>
          <option value="" disabled>
            Select a type…
          </option>
          {Object.entries(JOB_TYPES).map(([key, def]) => (
            <option key={key} value={key}>
              {def.label}
            </option>
          ))}
        </select>
        {selected && <span className="muted">{selected.description}</span>}
      </label>

      <JobPayloadFields
        jobType={selected}
        values={values}
        onChange={(name, val) => setValues((v) => ({ ...v, [name]: val }))}
      />

      <label>
        Priority
        <input
          type="number"
          min={MIN_PRIORITY}
          max={MAX_PRIORITY}
          value={priority}
          onChange={(e) => setPriority(e.target.value)}
        />
        <span className="muted">
          {MIN_PRIORITY}–{MAX_PRIORITY}; higher runs first.
        </span>
      </label>

      <button type="submit" disabled={submitting || !type}>
        {submitting ? "Submitting…" : "Submit"}
      </button>
    </form>
  );
}
