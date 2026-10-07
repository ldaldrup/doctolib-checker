const paths = {
  plus: "M12 5v14M5 12h14", search: "m20 20-4.3-4.3M18 10.8a7.2 7.2 0 1 1-14.4 0 7.2 7.2 0 0 1 14.4 0Z",
  bell: "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4",
  bellActive: "M18 9a6 6 0 0 0-12 0v3c0 2.3-1 4.2-2 5h16c-1-.8-2-2.7-2-5V9ZM10 20h4M4 3 2 5M20 3l2 2M2 9H1M23 9h-1",
  pause: "M8 5h3v14H8zM15 5h3v14h-3z", play: "m8 5 11 7-11 7V5Z",
  edit: "m4 20 4.2-.9L19 8.3 15.7 5 4.9 15.8 4 20ZM13.9 6.8l3.3 3.3",
  trash: "M4 7h16M10 4h4M7 7l1 13h8l1-13M10 10v7M14 10v7",
  list: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01",
  link: "M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1.1M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1.1",
  send: "m21 3-7.5 18-3.3-7.2L3 10.5 21 3ZM10.2 13.8 21 3",
  video: "M3 6h13v12H3zM16 10l5-3v10l-5-3",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20ZM12 6v6l4 2",
  check: "m5 12 4 4L19 6", close: "M5 5l14 14M19 5 5 19",
  calendar: "M4 6h16v15H4zM8 3v6M16 3v6M4 10h16",
  external: "M14 4h6v6M20 4l-9 9M19 14v5H5V5h5",
  info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20ZM12 10v7M12 7h.01",
  bolt: "m13 2-9 11h7l-1 9 10-12h-7V2Z",
  sliders: "M4 7h16M4 17h16M9 4v6M15 14v6", arrow: "M7 12h10m-4-4 4 4-4 4", chevronRight: "m9 5 7 7-7 7",
  warning: "m12 3 10 18H2L12 3ZM12 9v5M12 18h.01"
};

export function icon(name, className = "") {
  return `<svg class="${className}" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.info}"/></svg>`;
}

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}

export function formatInterval(seconds) {
  if (seconds == null || seconds === "" || !Number.isFinite(Number(seconds)) || Number(seconds) <= 0) return "Unknown";
  if (seconds % 3600 === 0) return `${seconds / 3600} hr`;
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} sec`;
}

export function relativeTime(iso) {
  if (!iso) return "Not checked yet";
  if (!Number.isFinite(Date.parse(iso))) return "Unknown checked time";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return `${hours} hr ago`;
}

export function formatDateTime(value, zone = "Europe/Berlin", {seconds = false} = {}) {
  if (value == null || value === "") return "Unknown time";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "Unknown time";
  const options = {timeZone: zone, day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
    ...(seconds ? {second: "2-digit"} : {})};
  try { return `${new Intl.DateTimeFormat("en-GB", options).format(date)} (${zone})`; }
  catch { return `${new Intl.DateTimeFormat("en-GB", {...options, timeZone: "UTC"}).format(date)} (UTC; time zone unavailable)`; }
}

export const formatSlot = (value, zone = "Europe/Berlin") => formatDateTime(value, zone);

export function formatDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return "Unknown date";
  const date = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== value) return "Unknown date";
  return new Intl.DateTimeFormat("en-GB", {timeZone: "UTC", day: "2-digit", month: "short", year: "numeric"}).format(date);
}

export function applyFieldErrors(form, error, output, aliases = {}, {focus = true} = {}) {
  if (!form) return null;
  for (const input of form.querySelectorAll("[data-field-error-id]")) {
    const ids = input.dataset.fieldErrorId.split(/\s+/);
    const descriptions = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter(value => value && !ids.includes(value));
    if (descriptions.length) input.setAttribute("aria-describedby", descriptions.join(" ")); else input.removeAttribute("aria-describedby");
    input.removeAttribute("aria-invalid"); delete input.dataset.fieldErrorId;
  }
  form.querySelectorAll("[data-generated-field-error]").forEach(node => node.remove());
  form.querySelectorAll("[data-error-for]").forEach(node => { node.hidden = true; node.textContent = ""; });
  if (output) { output.hidden = !error; output.textContent = error?.message || "Check the supplied values."; output.tabIndex = -1; }
  if (!error) return null;
  let first = null;
  let index = 0;
  for (const [field, message] of Object.entries(error.fields || {})) {
    const parts = field.split("."), base = parts[0];
    const names = aliases[field] || aliases[base] || base;
    let controls = [...form.elements].filter(input => input.name && (Array.isArray(names) ? names.includes(input.name) : input.name === names));
    if (/^\d+$/.test(parts[1] || "")) controls = controls.slice(Number(parts[1]), Number(parts[1]) + 1);
    if (!controls.length) continue;
    let notice = [...form.querySelectorAll("[data-error-for]")].find(node => node.dataset.errorFor === base);
    if (!notice) {
      notice = document.createElement("p"); notice.className = "form-error"; notice.dataset.generatedFieldError = "";
      (controls[0].closest(".field, .settings-control, .checkbox-line, .target-entry, fieldset") || controls[0].parentElement).append(notice);
    }
    notice.id ||= `${form.id || "form"}-field-error-${index++}`;
    notice.textContent = String(message); notice.hidden = false;
    for (const input of controls) {
      input.setAttribute("aria-invalid", "true");
      input.dataset.fieldErrorId = [...new Set([...(input.dataset.fieldErrorId || "").split(/\s+/).filter(Boolean), notice.id])].join(" ");
      const descriptions = new Set((input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
      descriptions.add(notice.id); input.setAttribute("aria-describedby", [...descriptions].join(" "));
      if (!first && !input.matches(":disabled") && input.type !== "hidden" && !input.closest("[hidden]")) first = input;
    }
  }
  if (first && focus) {
    for (let parent = first.parentElement; parent && parent !== form; parent = parent.parentElement) if (parent.tagName === "DETAILS") parent.open = true;
    first.focus();
  } else if (focus) output?.focus();
  return first;
}

export function badge(label, kind = "") {
  return `<span class="badge ${kind ? `badge-${kind}` : ""}">${kind ? '<span class="badge-dot" aria-hidden="true"></span>' : ""}${escapeHtml(label)}</span>`;
}
