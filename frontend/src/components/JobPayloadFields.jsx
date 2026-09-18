export function JobPayloadFields({ jobType, values, onChange }) {
  if (!jobType) return null;
  return (
    <>
      {jobType.fields.map((field) => (
        <label key={field.name}>
          {field.label}
          {field.required ? "" : " (optional)"}
          {field.type === "textarea" ? (
            <textarea
              value={values[field.name] ?? ""}
              onChange={(e) => onChange(field.name, e.target.value)}
              required={field.required}
              rows={field.rows ?? 5}
              placeholder={field.placeholder}
            />
          ) : (
            <input
              type={field.type}
              value={values[field.name] ?? ""}
              onChange={(e) => onChange(field.name, e.target.value)}
              required={field.required}
              maxLength={field.maxLength}
              placeholder={field.placeholder}
            />
          )}
          {field.help && <span className="muted">{field.help}</span>}
        </label>
      ))}
    </>
  );
}
