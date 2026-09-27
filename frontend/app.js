// Host/agent data (and, via plugins/config-diff, data that ultimately comes
// from a remote agent host) gets rendered with innerHTML template literals
// throughout this file — escape anything that isn't a literal we wrote
// ourselves before it goes into one of those templates.
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Every API call goes through here: JSON body in, a non-2xx response's
// `detail` out as a thrown Error.
async function request(path, method = "GET", body) {
  const init = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    // An unhandled server exception is a plain-text 500, not JSON.
    const detail = await r.json().then((b) => b.detail, () => null);
    throw new Error(detail || r.statusText);
  }
  return r;
}

async function json(path, method, body) {
  return (await request(path, method, body)).json();
}

const api = {
  list: (kind) => json(`/api/${kind}`),
  get: (kind, id) => json(`/api/${kind}/${id}`),
  put: (kind, id, body) => json(`/api/${kind}/${id}`, "PUT", body),
  del: (kind, id) => json(`/api/${kind}/${id}`, "DELETE"),
  agentStatus: (id) => json(`/api/agents/${id}/status`).catch((err) => ({ state: "unreachable", detail: err.message })),
  agentLogs: async (id, lines = 200) => (await json(`/api/agents/${id}/logs?lines=${lines}`)).text,
  reconcile: (id) => json(`/api/agents/${id}/reconcile`),
  applyFix: (id, fix) => json(`/api/agents/${id}/reconcile`, "POST", { fix, confirm: true }),
  configDiff: (id) => json(`/api/agents/${id}/config-diff`),
  restart: (id) => json(`/api/agents/${id}/restart?confirm=true`, "POST"),
  listPlugins: (id) => json(`/api/agents/${id}/plugins`),
  updatePlugin: (id, plugin) => json(`/api/agents/${id}/plugins/${plugin}/update?confirm=true`, "POST"),
  applyTemplate: (name, agentIds) => json(`/api/templates/${name}/apply`, "POST", { agent_ids: agentIds, confirm: true }),
  runCronJob: (id) => json(`/api/cron-jobs/${id}/run`, "POST"),
  dryRunCronJob: (id) => json(`/api/cron-jobs/${id}/dry-run`, "POST"),
};

const splitList = (value) => value.split(",").map((t) => t.trim()).filter(Boolean);

function statusPill(state) {
  const labels = { "not-installed": "not installed", crashlooping: "crash-looping" };
  const safeState = ["loading", "active", "inactive", "failed", "unreachable", "not-installed", "crashlooping", "starting", "stopping"].includes(state) ? state : "unknown";
  const label = labels[safeState] || safeState;
  return `<span class="status-pill ${safeState}"><span class="dot"></span>${esc(label)}</span>`;
}

// One finding row. `summary` and `extra` are markup — escape before passing.
function finding(severity, summary, extra = "") {
  return `<div class="finding ${esc(severity)}"><span class="sev"></span><span class="summary">${summary}</span>${extra}</div>`;
}

// Wire every [data-<attr>] button under `root` to handler(attrValue, button).
function bindActions(root, handlers) {
  for (const [attr, handler] of Object.entries(handlers)) {
    root.querySelectorAll(`[data-${attr}]`).forEach((b) =>
      b.addEventListener("click", () => handler(b.getAttribute(`data-${attr}`), b))
    );
  }
}

// Fill #<name>-body with one <tr> per item, toggle #<name>-empty.
function renderRows(name, items, rowHtml, handlers) {
  const body = document.getElementById(`${name}-body`);
  document.getElementById(`${name}-empty`).hidden = items.length > 0;
  body.innerHTML = items.map((item) => `<tr>${rowHtml(item)}</tr>`).join("");
  bindActions(body, handlers);
}

// POST to a text/plain streaming endpoint, appending output to `out` live.
async function streamInto(out, btn, path, body) {
  out.hidden = false;
  out.textContent = "";
  btn.disabled = true;
  try {
    const reader = (await request(path, "POST", body)).body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      out.textContent += decoder.decode(value, { stream: true });
      out.scrollTop = out.scrollHeight;
    }
  } catch (err) {
    out.textContent += `${out.textContent && "\n"}[error] ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

// ---- tabs ----
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(`${tab.dataset.tab}-view`).classList.add("active");
  });
});

// ---- modal helpers ----
function openModal(id) { document.getElementById(id).classList.add("open"); }
function closeModal(id) { document.getElementById(id).classList.remove("open"); }
document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => btn.closest(".modal-backdrop").classList.remove("open"));
});

// ---- hosts ----
async function renderHosts() {
  renderRows("hosts", await api.list("hosts"), (h) => `
      <td>${esc(h.id)}</td>
      <td>${esc(h.address)}</td>
      <td>${esc(h.ssh.user)}</td>
      <td>${esc(h.ssh.key ?? h.ssh.config_file)}</td>
      <td>${esc(h.ssh.port)}</td>
      <td>${esc(h.tags.join(", "))}</td>
      <td class="row-actions">
        <button class="link-btn" data-edit="${esc(h.id)}">Edit</button>
        <button class="link-btn danger" data-del="${esc(h.id)}">Delete</button>
      </td>`, { edit: editHost, del: deleteHost });
}

function fillHostForm(h) {
  const f = document.getElementById("host-form");
  f.id.value = h?.id ?? "";
  f.id.readOnly = !!h;
  f.address.value = h?.address ?? "";
  f.ssh_user.value = h?.ssh.user ?? "";
  f.ssh_port.value = h?.ssh.port ?? 22;
  f.ssh_key.value = h?.ssh.key ?? "";
  f.tags.value = h?.tags.join(", ") ?? "";
  document.getElementById("host-modal-title").textContent = h ? `Edit ${h.id}` : "Add host";
}

document.getElementById("add-host").addEventListener("click", () => {
  fillHostForm(null);
  openModal("host-modal");
});

async function editHost(id) {
  fillHostForm(await api.get("hosts", id));
  openModal("host-modal");
}

async function deleteHost(id) {
  if (!confirm(`Delete host ${id}? Agents referencing it will fail to deploy.`)) return;
  await api.del("hosts", id);
  renderHosts();
}

document.getElementById("host-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    id: f.id.value,
    address: f.address.value,
    ssh: { user: f.ssh_user.value, key: f.ssh_key.value, port: Number(f.ssh_port.value) },
    tags: splitList(f.tags.value),
  };
  try {
    await api.put("hosts", body.id, body);
    closeModal("host-modal");
    renderHosts();
    populateHostSelect();
  } catch (err) {
    alert(err.message);
  }
});

// ---- agents ----
async function populateHostSelect() {
  const hosts = await api.list("hosts");
  const select = document.getElementById("agent-host-select");
  const current = select.value;
  select.replaceChildren(...hosts.map((h) => {
    const option = document.createElement("option");
    option.value = h.id;
    option.textContent = h.id;
    return option;
  }));
  if (hosts.some((h) => h.id === current)) select.value = current;
}

async function renderAgents() {
  const agents = await api.list("agents");
  renderRows("agents", agents, (a) => `
      <td id="status-${esc(a.id)}">${statusPill("loading")}</td>
      <td>${esc(a.id)}</td>
      <td>${esc(a.type)}</td>
      <td>${esc(a.host)}</td>
      <td>${esc(a.profile ?? "")}</td>
      <td>${esc(a.owner ?? "")}</td>
      <td class="row-actions">
        <button class="link-btn" data-inspect="${esc(a.id)}">Inspect</button>
        <button class="link-btn" data-edit="${esc(a.id)}">Edit</button>
        <button class="link-btn danger" data-del="${esc(a.id)}">Delete</button>
      </td>`, { inspect: openInspect, edit: editAgent, del: deleteAgent });
  refreshStatuses(agents);
}

async function refreshStatuses(agents) {
  await Promise.all(
    agents.map(async (a) => {
      const cell = document.getElementById(`status-${a.id}`);
      const s = await api.agentStatus(a.id);
      if (cell) cell.innerHTML = statusPill(s.state);
    })
  );
}

let currentInspectId = null;

async function openInspect(id) {
  currentInspectId = id;
  document.getElementById("inspect-title").textContent = id;
  document.getElementById("inspect-logs").textContent = "—";
  document.getElementById("inspect-status").textContent = "loading…";
  for (const action of ["deploy", "restart", "update-agent", "config-push", "decommission"]) {
    const out = document.getElementById(`inspect-${action}-output`);
    out.hidden = true;
    out.textContent = "";
  }
  for (const panel of ["plugins", "reconcile", "config"]) {
    document.getElementById(`inspect-${panel}-results`).innerHTML = "";
  }
  openModal("inspect-modal");
  const s = await api.agentStatus(id);
  document.getElementById("inspect-status").textContent = [
    `state       ${s.state}`,
    s.active_state ? `active      ${s.active_state} / ${s.sub_state ?? ""}` : null,
    s.pid ? `pid         ${s.pid}` : null,
    s.since ? `since       ${s.since}` : null,
    s.detail ? `detail      ${s.detail}` : null,
  ].filter(Boolean).join("\n");
}

// Show a placeholder in #<elId>, then render load(currentInspectId) into it,
// or the error if it throws.
async function loadInto(elId, loadingMsg, load, render) {
  if (!currentInspectId) return;
  const el = document.getElementById(elId);
  el.innerHTML = finding("info", loadingMsg);
  try {
    render(await load(currentInspectId));
  } catch (err) {
    el.innerHTML = finding("critical", esc(err.message));
  }
}

document.getElementById("inspect-load-logs").addEventListener("click", async () => {
  if (!currentInspectId) return;
  const el = document.getElementById("inspect-logs");
  el.textContent = "loading…";
  try {
    el.textContent = (await api.agentLogs(currentInspectId)) || "(empty)";
  } catch (err) {
    el.textContent = `[error] ${err.message}`;
  }
});

const CONFIG_SEVERITY = { match: "ok", "missing-live": "warn", drift: "warn", present: "ok", missing: "warn" };

function renderConfigFindings(result) {
  const el = document.getElementById("inspect-config-results");
  if (!result.reachable) {
    el.innerHTML = finding("critical", esc(result.detail));
    return;
  }
  const rows = [
    ...result.config.map((c) => {
      const detail = c.status === "match" ? "" : ` — live: ${esc(JSON.stringify(c.live))}, desired: ${esc(JSON.stringify(c.desired))}`;
      // Schema guardrail: an "error" finding names a path push will refuse —
      // escalate it past plain drift so it can't be mistaken for a pushable
      // change; a "warn" (unknown top-level key) at least surfaces the detail.
      const schemaSev = c.schema === "error" ? "critical" : c.schema === "warn" ? "warn" : null;
      const schemaNote = c.schema_detail ? ` — ⚠ ${esc(c.schema_detail)}` : "";
      return finding(schemaSev || CONFIG_SEVERITY[c.status] || "info", `<code>${esc(c.path)}</code> ${esc(c.status)}${detail}${schemaNote}`);
    }),
    ...result.env.map((e) => finding(CONFIG_SEVERITY[e.status] || "info", `<code>${esc(e.key)}</code> ${esc(e.status)} in .env`)),
  ];
  el.innerHTML = rows.length ? rows.join("") : finding("info", "nothing declared in desired.config / env_keys");
}

function runConfigCheck() {
  return loadInto("inspect-config-results", "checking…", api.configDiff, renderConfigFindings);
}

document.getElementById("inspect-config-diff").addEventListener("click", runConfigCheck);

document.getElementById("inspect-config-push").addEventListener("click", async (e) => {
  if (!currentInspectId) return;
  if (!confirm(`Push desired config to ${currentInspectId}? Runs "hermes config set" for every declared key, then restarts the gateway if it's active.`)) return;
  await streamInto(document.getElementById("inspect-config-push-output"), e.target, `/api/agents/${currentInspectId}/config-diff?confirm=true`);
  runConfigCheck();
});

function renderFindings(findings) {
  const el = document.getElementById("inspect-reconcile-results");
  el.innerHTML = findings.map((f) => finding(f.severity, esc(f.summary),
    f.fix ? `<button type="button" class="link-btn" data-fix="${esc(f.fix)}">Fix: ${esc(f.fix)}</button>` : ""
  )).join("");
  bindActions(el, { fix: runFix });
}

function runCheck() {
  return loadInto("inspect-reconcile-results", "checking…", api.reconcile, renderFindings);
}

async function runFix(fix, btn) {
  if (!currentInspectId) return;
  if (!confirm(`Apply fix "${fix}" to ${currentInspectId}?`)) return;
  btn.disabled = true;
  btn.textContent = "applying…";
  try {
    const result = await api.applyFix(currentInspectId, fix);
    if (!result.ok) alert(`Fix ran but reported an error:\n${result.output}`);
  } catch (err) {
    alert(err.message);
  }
  await runCheck();
  refreshStatuses(await api.list("agents"));
}

document.getElementById("inspect-reconcile").addEventListener("click", runCheck);

document.getElementById("inspect-deploy").addEventListener("click", async (e) => {
  if (!currentInspectId) return;
  if (!confirm(`Deploy ${currentInspectId}? This runs install/config/service commands on the host.`)) return;
  await streamInto(document.getElementById("inspect-deploy-output"), e.target, `/api/agents/${currentInspectId}/deploy?confirm=true`);
  refreshStatuses(await api.list("agents"));
});

document.getElementById("inspect-restart").addEventListener("click", async (e) => {
  if (!currentInspectId) return;
  if (!confirm(`Restart ${currentInspectId}'s gateway?`)) return;
  const btn = e.target;
  const out = document.getElementById("inspect-restart-output");
  out.hidden = false;
  out.textContent = "restarting…";
  btn.disabled = true;
  try {
    const result = await api.restart(currentInspectId);
    out.textContent = result.output || (result.ok ? "done" : "failed");
  } catch (err) {
    out.textContent = `[error] ${err.message}`;
  } finally {
    btn.disabled = false;
    refreshStatuses(await api.list("agents"));
  }
});

const PLUGIN_SEVERITY = { enabled: "ok", disabled: "warn" };

function renderPlugins(plugins) {
  const el = document.getElementById("inspect-plugins-results");
  el.innerHTML = plugins.length
    ? plugins.map((p) => finding(PLUGIN_SEVERITY[p.status] || "info",
        `<code>${esc(p.name)}</code> v${esc(p.version)} — ${esc(p.status)} (${esc(p.source)})`,
        p.source === "git" ? `<button type="button" class="link-btn" data-update-plugin="${esc(p.name)}">Update</button>` : ""
      )).join("")
    : finding("info", "no plugins");
  bindActions(el, { "update-plugin": runUpdatePlugin });
}

function runListPlugins() {
  return loadInto("inspect-plugins-results", "loading…", api.listPlugins, renderPlugins);
}

document.getElementById("inspect-plugins-list").addEventListener("click", runListPlugins);

async function runUpdatePlugin(plugin, btn) {
  if (!currentInspectId) return;
  if (!confirm(`Update plugin "${plugin}" on ${currentInspectId}? Note: if the update trips Hermes's own security scan, it can auto-disable the plugin — including a live messaging platform.`)) return;
  btn.disabled = true;
  btn.textContent = "updating…";
  try {
    const result = await api.updatePlugin(currentInspectId, plugin);
    if (result.disabled_by_scan) {
      alert(`Update ran, but Hermes's security scan flagged the new code and auto-disabled "${plugin}". If it served a messaging platform, that platform is now down. Review the findings on the host, then re-enable manually if you trust them.\n\n${result.output}`);
    } else if (!result.ok) {
      alert(`Update failed:\n${result.output}`);
    }
  } catch (err) {
    alert(err.message);
  }
  await runListPlugins();
}

document.getElementById("inspect-update-agent").addEventListener("click", async (e) => {
  if (!currentInspectId) return;
  if (!confirm(`Update hermes itself on ${currentInspectId}'s host? This updates the shared code checkout — every profile on that install is affected, not just this one.`)) return;
  await streamInto(document.getElementById("inspect-update-agent-output"), e.target, `/api/agents/${currentInspectId}/update?confirm=true`);
});

document.getElementById("inspect-decommission").addEventListener("click", async (e) => {
  if (!currentInspectId) return;
  const id = currentInspectId;
  if (!confirm(`Decommission ${id}? This stops and uninstalls its gateway service on the host, then archives its Beacon record.`)) return;

  const agent = await api.get("agents", id);
  let purge = false;
  if (agent.profile && agent.profile !== "default") {
    purge = confirm(
      `Also delete ${id}'s profile data (memory, sessions, skills)? This cannot be undone.\n\n` +
      `OK = delete the data too. Cancel = keep the data, just remove the service.`
    );
  }
  let removeUser = false;
  if (agent.desired && agent.desired.os_user) {
    removeUser = confirm(
      `This agent runs under OS user "${agent.desired.os_user}". Also delete that user account entirely (userdel -r)?\n\n` +
      `OK = delete the account. Cancel = leave the account, just remove the service.`
    );
  }

  await streamInto(document.getElementById("inspect-decommission-output"), e.target,
    `/api/agents/${id}/decommission`, { purge, remove_user: removeUser, confirm: true });
  closeModal("inspect-modal");
  renderAgents();
});

// ---- templates ----
async function renderTemplates() {
  renderRows("templates", await api.list("templates"), (t) => `
      <td>${esc(t.name)}</td>
      <td>${t.used_by.length ? esc(t.used_by.join(", ")) : '<span class="hint">—</span>'}</td>
      <td class="row-actions">
        <button class="link-btn" data-view="${esc(t.name)}">View</button>
        <button class="link-btn" data-apply="${esc(t.name)}">Apply to…</button>
      </td>`, { view: openTemplateView, apply: openTemplateApply });
}

async function openTemplateView(name) {
  document.getElementById("template-view-title").textContent = name;
  const el = document.getElementById("template-view-body");
  el.textContent = "loading…";
  openModal("template-view-modal");
  try {
    el.textContent = JSON.stringify((await api.get("templates", name)).content, null, 2);
  } catch (err) {
    el.textContent = `[error] ${err.message}`;
  }
}

let applyTemplateName = null;

async function openTemplateApply(name) {
  applyTemplateName = name;
  document.getElementById("template-apply-title").textContent = `Apply "${name}" to agents`;
  const list = document.getElementById("template-apply-list");
  list.textContent = "loading…";
  openModal("template-apply-modal");
  try {
    const [agents, tpl] = await Promise.all([api.list("agents"), api.get("templates", name)]);
    const using = new Set(tpl.used_by);
    list.innerHTML = agents.length
      ? agents.map((a) => `
        <label>
          <input type="checkbox" value="${esc(a.id)}" ${using.has(a.id) ? "checked disabled" : ""}>
          ${esc(a.id)}
          <span class="mut">${esc(a.host)}${a.profile ? " / " + esc(a.profile) : ""}${using.has(a.id) ? " — already applied" : ""}</span>
        </label>`).join("")
      : '<span class="hint">no agents</span>';
  } catch (err) {
    list.textContent = `[error] ${err.message}`;
  }
}

document.getElementById("template-apply-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ids = [...document.querySelectorAll("#template-apply-list input:checked:not(:disabled)")].map((c) => c.value);
  if (!ids.length) { alert("Pick at least one agent."); return; }
  if (!confirm(`Add template "${applyTemplateName}" to ${ids.length} agent(s): ${ids.join(", ")}?`)) return;
  try {
    await api.applyTemplate(applyTemplateName, ids);
    closeModal("template-apply-modal");
    renderTemplates();
  } catch (err) {
    alert(err.message);
  }
});

document.querySelector('[data-tab="templates"]').addEventListener("click", renderTemplates);

// ---- cron jobs ----
async function renderCronJobs() {
  renderRows("cron-jobs", await api.list("cron-jobs"), (j) => `
      <td><input type="checkbox" ${j.enabled ? "checked" : ""} disabled></td>
      <td>${esc(j.id)}</td>
      <td><code>${esc(j.schedule)}</code> ${esc(j.timezone ?? "UTC")}</td>
      <td>${esc(j.command?.action ?? "")}</td>
      <td>${esc((j.target_agent_ids ?? []).join(", "))}</td>
      <td>${j.last_run_at
        ? esc(`${j.last_run_status || "unknown"} — ${new Date(j.last_run_at).toLocaleString()}`)
        : '<span class="hint">—</span>'}</td>
      <td class="row-actions">
        <button class="link-btn" data-run="${esc(j.id)}">Run now</button>
        <button class="link-btn" data-dry="${esc(j.id)}">Dry run</button>
      </td>
      <td class="row-actions">
        <button class="link-btn" data-view="${esc(j.id)}">View</button>
        <button class="link-btn" data-edit="${esc(j.id)}">Edit</button>
        <button class="link-btn danger" data-del="${esc(j.id)}">Delete</button>
      </td>`,
    { run: runCronJobNow, dry: dryRunCronJob, view: openCronJobView, edit: editCronJob, del: deleteCronJob });
}

function fillCronJobForm(j) {
  const f = document.getElementById("cron-job-form");
  f.id.value = j?.id ?? "";
  f.id.readOnly = !!j;
  f.schedule.value = j?.schedule ?? "";
  f.timezone.value = j?.timezone ?? "UTC";
  f.enabled.value = String(j?.enabled ?? true);
  f.action.value = j?.command?.action ?? "restart";
  f.target_agent_ids.value = (j?.target_agent_ids ?? []).join(", ");
  f.timeout.value = j?.timeout ?? "";
  f.owner.value = j?.owner ?? "";
  f.notes.value = j?.notes ?? "";
  document.getElementById("cron-job-modal-title").textContent = j ? `Edit ${j.id}` : "Add cron job";
}

document.getElementById("add-cron-job").addEventListener("click", () => {
  fillCronJobForm(null);
  openModal("cron-job-modal");
});

async function editCronJob(id) {
  fillCronJobForm(await api.get("cron-jobs", id));
  openModal("cron-job-modal");
}

async function deleteCronJob(id) {
  if (!confirm(`Delete cron job ${id}?`)) return;
  await api.del("cron-jobs", id);
  renderCronJobs();
}

async function openCronJobView(id) {
  document.getElementById("cron-job-view-title").textContent = id;
  const meta = document.getElementById("cron-job-view-meta");
  const out = document.getElementById("cron-job-view-output");
  out.textContent = "loading…";
  openModal("cron-job-view-modal");
  try {
    const j = await api.get("cron-jobs", id);
    meta.textContent = [
      `enabled: ${j.enabled}`,
      `schedule: ${j.schedule} (${j.timezone ?? "UTC"})`,
      `action: ${j.command?.action ?? ""}`,
      `targets: ${(j.target_agent_ids ?? []).join(", ")}`,
      `last_run_status: ${j.last_run_status ?? "—"}`,
      j.last_run_at ? `last_run_at: ${new Date(j.last_run_at).toISOString()}` : null,
      j.owner ? `owner: ${j.owner}` : null,
      j.notes ? `notes: ${j.notes}` : null,
    ].filter(Boolean).join("\n");
    out.textContent = j.last_run_output || "No run output yet.";
  } catch (err) {
    meta.textContent = "";
    out.textContent = `[error] ${err.message}`;
  }
}

document.getElementById("cron-job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    id: f.id.value,
    enabled: f.enabled.value === "true",
    schedule: f.schedule.value,
    timezone: f.timezone.value || "UTC",
    command: { action: f.action.value },
    target_agent_ids: splitList(f.target_agent_ids.value),
    owner: f.owner.value || null,
    notes: f.notes.value || null,
  };
  const timeout = f.timeout.value.trim();
  if (timeout) body.timeout = Number(timeout);
  try {
    await api.put("cron-jobs", body.id, body);
    closeModal("cron-job-modal");
    renderCronJobs();
  } catch (err) {
    alert(err.message);
  }
});

async function runCronJobNow(id, btn) {
  if (!confirm(`Run cron job "${id}" now? This executes against its target agents immediately.`)) return;
  btn.disabled = true;
  btn.textContent = "running…";
  try {
    const result = await api.runCronJob(id);
    openCronJobView(id);
    if (result.status !== "ok") alert(`Job finished with status: ${result.status}`);
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run now";
    renderCronJobs();
  }
}

async function dryRunCronJob(id, btn) {
  btn.disabled = true;
  btn.textContent = "checking…";
  try {
    const result = await api.dryRunCronJob(id);
    alert(`"${id}" is ${result.due ? "due" : "not due"} right now (${result.schedule}).`);
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Dry run";
  }
}

document.querySelector('[data-tab="cron-jobs"]').addEventListener("click", renderCronJobs);

async function fillAgentForm(a) {
  await populateHostSelect();
  const f = document.getElementById("agent-form");
  f.id.value = a?.id ?? "";
  f.id.readOnly = !!a;
  f.type.value = a?.type ?? "hermes";
  f.profile.value = a?.profile ?? "";
  f.host.value = a?.host ?? f.host.value;
  f.owner.value = a?.owner ?? "";
  f.notes.value = a?.notes ?? "";
  f.templates.value = (a?.templates ?? []).join(", ");
  f.desired.value = JSON.stringify(
    a?.desired ?? { install_mode: "simple", os_user: "", service: "", log_path: "", config: {}, env_keys: [] },
    null, 2
  );
  document.getElementById("agent-modal-title").textContent = a ? `Edit ${a.id}` : "Add agent";
}

document.getElementById("add-agent").addEventListener("click", async () => {
  await fillAgentForm(null);
  openModal("agent-modal");
});

async function editAgent(id) {
  // From the list, not GET /api/agents/{id}: that one resolves templates, so
  // an agent naming a deleted template would 404 exactly when it needs fixing.
  const agents = await api.list("agents");
  await fillAgentForm(agents.find((a) => a.id === id));
  openModal("agent-modal");
}

async function deleteAgent(id) {
  if (!confirm(`Delete agent ${id}?`)) return;
  await api.del("agents", id);
  renderAgents();
}

document.getElementById("agent-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  let desired;
  try {
    desired = JSON.parse(f.desired.value);
  } catch {
    alert("Desired config must be valid JSON.");
    return;
  }
  const body = {
    id: f.id.value,
    type: f.type.value,
    host: f.host.value,
    profile: f.profile.value || null,
    owner: f.owner.value || null,
    notes: f.notes.value || null,
    templates: splitList(f.templates.value),
    desired,
  };
  try {
    await api.put("agents", body.id, body);
    closeModal("agent-modal");
    renderAgents();
    renderTemplates();
  } catch (err) {
    alert(err.message);
  }
});

renderHosts();
renderAgents();
renderTemplates();
renderCronJobs();
populateHostSelect();

fetch("/auth/me").then((r) => (r.ok ? r.json() : null)).then((user) => {
  if (!user) return; // auth disabled — nothing to show
  document.getElementById("whoami-name").textContent = user.name || user.email || user.sub;
  document.getElementById("whoami").hidden = false;
});

setInterval(() => {
  if (document.getElementById("agents-view").classList.contains("active")) renderAgents();
}, 30000);
