import { escapeHtml as h } from "./ui.js";

export function deliveryNotice(state) {
  if (state.load?.status?.phase !== "loaded") return "Notification delivery status is unknown. Availability checking has a separate worker.";
  const status = state.status || {};
  if (!status.telegram_configured) return "Telegram is not configured on the server.";
  const backlog = status.delivery_backlog || {};
  const notices = [];
  if (!status.dispatcher_alive) notices.push("Notification dispatcher unavailable; queued alerts are retained. Availability checking runs independently.");
  else notices.push("Notification dispatcher online.");
  const queued = backlog.queued ?? ((backlog.ready || 0) + (backlog.retry || 0));
  if (queued) notices.push(`${queued} alert(s) queued or awaiting fresh confirmation.`);
  if (backlog.in_flight) notices.push(`${backlog.in_flight} delivery attempt(s) in progress.`);
  if (backlog.action_required) notices.push(`${backlog.action_required} alert(s) need credential or recipient repair and explicit recovery.`);
  if (backlog.exhausted) notices.push(`${backlog.exhausted} alert(s) reached their retry limit and need explicit recovery.`);
  if (backlog.uncertain) notices.push(`${backlog.uncertain} alert(s) may already have been delivered. Resending requires acknowledgement of duplicate risk.`);
  return notices.join(" ");
}

export function renderDeliveryNotice(state) {
  return `<div class="notice ${state.status?.dispatcher_alive ? "" : "notice-warning"}" role="status"><span>${h(deliveryNotice(state))}</span></div>`;
}
