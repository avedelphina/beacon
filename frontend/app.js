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
// A validation failure comes as a list of {loc, msg}, not a string: say which
// field and why, instead of "[object Object]".
function describe(detail) {
  if (typeof detail === "string" || detail == null) return detail;
  if (!Array.isArray(detail)) return JSON.stringify(detail);
  return detail.map((d) => {
    const field = (d.loc ?? []).filter((p) => p !== "body").join(".");
    const msg = String(d.msg ?? JSON.stringify(d)).replace(/^Value error, /, "");
    return field ? `${field}: ${msg}` : msg;
  }).join("\n");
}

async function request(path, method = "GET", body) {
  const init = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const r = await fetch(path, init);
  if (r.status === 401) {
    // The session ran out: go and sign in again instead of failing call by call.
    location.href = "/auth/login";
    throw new Error("Your session expired. Signing in again…");
  }
  if (!r.ok) {
    // An unhandled server exception is a plain-text 500, not JSON.
    const detail = await r.json().then((b) => b.detail, () => null);
    throw new Error(describe(detail) || r.statusText);
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
  availability: () => json("/api/availability"),
  agentStatus: (id) => json(`/api/agents/${id}/status`).catch((err) => ({ state: "unreachable", detail: err.message })),
  agentLogs: async (id, lines = 200) => (await json(`/api/agents/${id}/logs?lines=${lines}`)).text,
  reconcile: (id) => json(`/api/agents/${id}/reconcile`),
  applyFix: (id, fix) => json(`/api/agents/${id}/reconcile`, "POST", { fix, confirm: true }),
  configDiff: (id) => json(`/api/agents/${id}/config-diff`),
  pushConfig: (id, workflow) => {
    const params = new URLSearchParams({ confirm: "true", workflow_request_id: String(workflow.workflow_request_id), plan_revision: workflow.plan_revision, plan_hash: workflow.plan_hash });
    return `/api/agents/${id}/config-diff?${params}`;
  },
  restart: (id) => json(`/api/agents/${id}/restart?confirm=true`, "POST"),
  listSecrets: (id) => json(`/api/agents/${id}/secrets`),
  putSecret: (id, name, value) => json(`/api/agents/${id}/secrets`, "POST", { name, value, confirm: true }),
  revealSecret: (id, name) => json(`/api/agents/${id}/secrets/${encodeURIComponent(name)}/reveal?confirm=true`, "POST"),
  restartAgent: (id) => json(`/api/agents/${id}/restart-agent?confirm=true`, "POST"),
  listPlugins: (id) => json(`/api/agents/${id}/plugins`),
  updatePlugin: (id, plugin) => json(`/api/agents/${id}/plugins/${plugin}/update?confirm=true`, "POST"),
  applyTemplate: (name, agentIds) => json(`/api/templates/${name}/apply`, "POST", { agent_ids: agentIds, confirm: true }),
  listWorkflows: () => json("/api/workflows?include_done=false"),
  respondWorkflow: (id, body) => json(`/api/workflows/${id}/respond`, "POST", body),
  runCronJob: (id) => json(`/api/cron-jobs/${id}/run`, "POST"),
  dryRunCronJob: (id) => json(`/api/cron-jobs/${id}/dry-run`, "POST"),
};

const splitList = (value) => value.split(",").map((t) => t.trim()).filter(Boolean);

function statusPill(state, title = "") {
  const labels = { "not-installed": "not installed", crashlooping: "crash-looping", "sync-only": "sync only", "no-a2a": "no A2A", client: "client" };
  const safeState = ["loading", "active", "inactive", "failed", "unreachable", "not-installed", "crashlooping", "starting", "stopping",
    "online", "sync-only", "away", "offline", "no-a2a", "client"].includes(state) ? state : "unknown";
  const label = labels[safeState] || safeState;
  return `<span class="status-pill ${safeState}"${title ? ` title="${esc(title)}"` : ""}><span class="dot"></span>${esc(label)}</span>`;
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
  // On a phone each row becomes a card, and a card has no header row: every
  // cell carries its column's name (see styles.css, max-width 720px).
  const heads = [...document.querySelectorAll(`#${name}-view thead th`)].map((th) => th.textContent.trim());
  body.querySelectorAll("tr").forEach((tr) => tr.querySelectorAll("td").forEach((td, i) => { td.dataset.label = heads[i] ?? ""; }));
  bindActions(body, handlers);
}

// For a choice that cannot be undone: Enter on an empty box must not mean yes,
// so the operator types the name of what they are about to delete.
function typedConfirm(message, expected) {
  return (prompt(message, "") ?? "").trim() === expected;
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
      <td>${h.ssh ? esc(h.ssh.user) : '<span class="hint">no SSH (A2A only)</span>'}</td>
      <td>${esc(h.ssh?.key ?? h.ssh?.config_file ?? "")}</td>
      <td>${esc(h.ssh?.port ?? "")}</td>
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
  f.ssh_user.value = h?.ssh?.user ?? "";
  f.ssh_port.value = h?.ssh?.port ?? 22;
  f.ssh_key.value = h?.ssh?.key ?? "";
  f.ssh_config_file.value = h?.ssh?.config_file ?? "";
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
  if (!confirm(`Delete host ${id}?`)) return;
  try {
    await api.del("hosts", id);  // refused while live agents still use it
  } catch (err) {
    alert(err.message);
  }
  renderHosts();
}

document.getElementById("host-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    id: f.id.value,
    address: f.address.value,
    // No SSH user: a host Beacon only reaches its agents on over A2A.
    // Exactly one of key and config file (a Teleport tbot host has the file); empty means none.
    ssh: f.ssh_user.value ? {
      user: f.ssh_user.value, key: f.ssh_key.value.trim() || null,
      config_file: f.ssh_config_file.value.trim() || null, port: Number(f.ssh_port.value),
    } : null,
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

// ---- agents ----
let allAgents = [];
let agentFilter = null;            // null = all, "" = untagged, otherwise one tag
const agentSelection = new Set();  // ids ticked for bulk tagging; survives the 30 s refresh

const tagsOf = (a) => a.tags ?? [];
const uniqueTags = (agents) => [...new Set(agents.flatMap(tagsOf))].sort();
const tagChips = (tags) => (tags.length ? tags.map((t) => `<span class="chip">${esc(t)}</span>`).join(" ") : '<span class="hint">–</span>');

function renderTagBar() {
  const bar = document.getElementById("agent-tag-bar");
  const tags = uniqueTags(allAgents);
  const untagged = allAgents.filter((a) => !tagsOf(a).length).length;
  if (!tags.length) { bar.innerHTML = ""; return; }
  const chip = (value, label, count) =>
    `<button type="button" class="chip chip-btn${agentFilter === value ? " active" : ""}" data-filter="${esc(value ?? "__all__")}">${esc(label)} <span class="hint">${count}</span></button>`;
  bar.innerHTML = chip(null, "all", allAgents.length)
    + tags.map((t) => chip(t, t, allAgents.filter((a) => tagsOf(a).includes(t)).length)).join("")
    + (untagged ? chip("", "untagged", untagged) : "");
  bar.querySelectorAll("[data-filter]").forEach((b) => b.addEventListener("click", () => {
    const v = b.getAttribute("data-filter");
    agentFilter = v === "__all__" ? null : v;
    renderAgentTable();
  }));
}

function updateBulkBar() {
  const bar = document.getElementById("agent-bulk-bar");
  bar.hidden = agentSelection.size === 0;
  document.getElementById("agent-bulk-count").textContent = `${agentSelection.size} selected`;
}

// The agent's host reports chat settings that let anyone message it; Beacon
// then withholds the board from it even with reach.board on.
function openChatChip(a) {
  if (!a.chat_open?.length) return "";
  const withheld = a.reach?.board;
  return ` <span class="chip chip-warn" title="Open to anyone: ${esc(a.chat_open.join(", "))}${withheld ? ". Board withheld until it is locked down." : ""}">${withheld ? "board withheld" : "open chat"}</span>`;
}

function renderAgentTable() {
  renderTagBar();
  const shown = allAgents.filter((a) => agentFilter === null || (agentFilter === "" ? !tagsOf(a).length : tagsOf(a).includes(agentFilter)));
  renderRows("agents", shown, (a) => `
      <td class="sel op-only"><input type="checkbox" data-select="${esc(a.id)}" aria-label="Select ${esc(a.id)}"${agentSelection.has(a.id) ? " checked" : ""}></td>
      <td id="status-${esc(a.id)}">${statusPill("loading")}</td>
      <td>${esc(a.id)}${openChatChip(a)}</td>
      <td>${tagChips(tagsOf(a))}</td>
      <td>${esc(a.type)}</td>
      <td>${esc(a.host)}</td>
      <td>${esc(a.profile ?? "")}</td>
      <td>${esc(a.owner ?? "")}</td>
      <td class="row-actions">
        <button class="link-btn" data-inspect="${esc(a.id)}">Inspect</button>
        ${a.reach ? `<button class="link-btn op-only" data-enroll="${esc(a.id)}">Enroll</button>` : ""}
        <button class="link-btn op-only" data-edit="${esc(a.id)}">Edit</button>
        <button class="link-btn danger op-only" data-del="${esc(a.id)}">Delete</button>
      </td>`, { inspect: openInspect, enroll: openEnroll, edit: editAgent, del: deleteAgent });
  document.querySelectorAll("#agents-body [data-select]").forEach((cb) => cb.addEventListener("change", () => {
    if (cb.checked) agentSelection.add(cb.dataset.select); else agentSelection.delete(cb.dataset.select);
    updateBulkBar();
  }));
  updateBulkBar();
  refreshStatuses(shown);
}

async function renderAgents() {
  allAgents = await api.list("agents");
  for (const id of [...agentSelection]) if (!allAgents.some((a) => a.id === id)) agentSelection.delete(id);
  document.getElementById("tag-names").innerHTML = uniqueTags(allAgents).map((t) => `<option value="${esc(t)}">`).join("");
  renderAgentTable();
}

document.getElementById("agent-bulk-clear").addEventListener("click", () => { agentSelection.clear(); renderAgentTable(); });
document.getElementById("agent-bulk-tag").addEventListener("click", () => {
  document.getElementById("bulk-tag-targets").textContent = `For: ${[...agentSelection].join(", ")}`;
  document.getElementById("bulk-tag-form").reset();
  openModal("bulk-tag-modal");
});
document.getElementById("bulk-tag-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tag = e.target.tag.value.trim();
  const adding = e.submitter?.value !== "remove";
  const failures = [];
  for (const id of agentSelection) {
    const a = allAgents.find((x) => x.id === id);
    if (!a) continue;
    const tags = adding ? [...new Set([...tagsOf(a), tag])] : tagsOf(a).filter((t) => t !== tag);
    if (tags.length === tagsOf(a).length && tags.every((t) => tagsOf(a).includes(t))) continue;  // already so
    try {
      await api.put("agents", id, { ...a, tags });  // the whole record, so nothing else changes
    } catch (err) {
      failures.push(`${id}: ${err.message}`);
    }
  }
  closeModal("bulk-tag-modal");
  agentSelection.clear();
  await renderAgents();
  if (failures.length) alert(`Some agents were not changed:\n${failures.join("\n")}`);
});

// The pill is whether the agent answers over A2A (or its host still syncs),
// not whether Beacon can SSH in: one call for the whole fleet.
async function refreshStatuses(agents) {
  let all = {};
  try {
    all = await api.availability();
  } catch (err) {
    all = {};
  }
  for (const a of agents) {
    const cell = document.getElementById(`status-${a.id}`);
    const s = all[a.id];
    if (cell) cell.innerHTML = s ? statusPill(s.state, s.detail) + (s.sync === "outdated"
      ? ' <span class="chip chip-warn" title="This host runs an older sync script than Beacon serves. Turn on update_sync in the policy, or install it again.">sync outdated</span>' : "")
      : statusPill("unknown", "availability could not be loaded");
  }
}

let currentInspectId = null;
let currentConfigWorkflow = null;

async function openInspect(id) {
  currentInspectId = id;
  currentConfigWorkflow = null;
  document.getElementById("inspect-title").textContent = id;
  document.getElementById("inspect-logs").textContent = "—";
  document.getElementById("inspect-status").textContent = "loading…";
  for (const action of ["deploy", "restart", "update-agent", "config-push", "decommission"]) {
    const out = document.getElementById(`inspect-${action}-output`);
    out.hidden = true;
    out.textContent = "";
  }
  clearSecrets();
  for (const panel of ["plugins", "reconcile", "config"]) {
    document.getElementById(`inspect-${panel}-results`).innerHTML = "";
  }
  const mine = isMember ? allAgents.find((x) => x.id === id) : null;
  document.getElementById("inspect-logs-section").hidden = !!mine && !mine.member_logs;
  openModal("inspect-modal");
  const [s, avail] = await Promise.all([api.agentStatus(id), api.availability().then((all) => all[id]).catch(() => null)]);
  document.getElementById("inspect-status").textContent = [
    avail ? `A2A         ${avail.state}${avail.detail ? ` (${avail.detail})` : ""}` : null,
    `service     ${s.state} (over SSH)`,
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

// A secret stays on the page only while its agent's dialog is open and the page is in front.
function clearSecrets() {
  const shown = document.getElementById("inspect-secret-value");
  shown.textContent = "";
  shown.hidden = true;
  document.getElementById("inspect-secrets-results").innerHTML = "";
  document.getElementById("inspect-secret-form").reset();
}
document.querySelectorAll("#inspect-modal [data-close]").forEach((b) => b.addEventListener("click", clearSecrets));
document.addEventListener("visibilitychange", () => { if (document.hidden) clearSecrets(); });

document.getElementById("inspect-secrets-list").addEventListener("click", () =>
  loadInto("inspect-secrets-results", "asking the host…", api.listSecrets, ({ names }) => {
    const el = document.getElementById("inspect-secrets-results");
    el.innerHTML = names.length
      ? names.map((n) => finding("info", esc(n), `<button type="button" class="link-btn" data-reveal="${esc(n)}">Reveal</button>`)).join("")
      : finding("ok", "nothing waiting");
    el.querySelectorAll("[data-reveal]").forEach((b) => b.addEventListener("click", async () => {
      const name = b.dataset.reveal;
      if (!confirm(`Show ${name} and remove it from the agent's host? It cannot be shown again.`)) return;
      const shown = document.getElementById("inspect-secret-value");
      try {
        shown.textContent = (await api.revealSecret(currentInspectId, name)).value;
        shown.hidden = false;
        b.remove();
      } catch (err) {
        alert(err.message);
      }
    }));
  }));

document.getElementById("inspect-secret-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const id = currentInspectId;
  try {
    const { path } = await api.putSecret(id, form.elements.name.value.trim(), form.elements.value.value);
    form.reset();
    document.getElementById("inspect-secrets-results").innerHTML = finding("ok", `saved as ${esc(path)}`);
  } catch (err) {
    alert(err.message);
  }
});

const CONFIG_SEVERITY = { match: "ok", "missing-live": "warn", drift: "warn", present: "ok", missing: "warn" };

function renderConfigFindings(result) {
  currentConfigWorkflow = result.workflow || null;
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

async function loadConfigDiff(id) {
  const result = await api.configDiff(id);
  const requests = await api.listWorkflows();
  const approved = requests.find((w) => w.state === "approved"
    && w.capability === "push_config"
    && w.plan_revision === result.workflow?.plan_revision
    && w.plan_hash === result.workflow?.plan_hash);
  if (approved) result.workflow = { ...result.workflow, workflow_request_id: approved.id };
  return result;
}

function runConfigCheck() {
  return loadInto("inspect-config-results", "checking…", loadConfigDiff, renderConfigFindings);
}

document.getElementById("inspect-config-diff").addEventListener("click", runConfigCheck);

document.getElementById("inspect-config-push").addEventListener("click", async (e) => {
  if (!currentInspectId) return;
  if (!confirm(`Push desired config to ${currentInspectId}? Runs "hermes config set" for every declared key, then restarts the gateway if it's active.`)) return;
  if (!currentConfigWorkflow?.workflow_request_id) {
    alert("Approve the exact push plan in Workflows before pushing config.");
    return;
  }
  await streamInto(document.getElementById("inspect-config-push-output"), e.target, api.pushConfig(currentInspectId, currentConfigWorkflow));
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

function wireRestart(btnId, prompt, call) {
  document.getElementById(btnId).addEventListener("click", async (e) => {
    if (!currentInspectId) return;
    if (!confirm(prompt(currentInspectId))) return;
    const btn = e.target;
    const out = document.getElementById("inspect-restart-output");
    out.hidden = false;
    out.textContent = "restarting…";
    btn.disabled = true;
    try {
      const result = await call(currentInspectId);
      out.textContent = result.output || (result.ok ? "done" : "failed");
    } catch (err) {
      out.textContent = `[error] ${err.message}`;
    } finally {
      btn.disabled = false;
      refreshStatuses(await api.list("agents"));
    }
  });
}

wireRestart("inspect-restart", (id) => `Restart ${id}'s gateway?`, api.restart);
wireRestart("inspect-restart-agent",
  (id) => `Hard-restart ${id}? Restarts its Docker container (desired.container) or its systemd unit, without a graceful drain. Live sessions are dropped.`,
  api.restartAgent);

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
    purge = typedConfirm(
      `Also delete ${id}'s profile data (memory, sessions, skills)? This cannot be undone.\n\n` +
      `Type the agent id to delete the data too. Anything else keeps the data and only removes the service.`, id);
  }
  let removeUser = false;
  if (agent.desired && agent.desired.os_user) {
    removeUser = typedConfirm(
      `This agent runs under OS user "${agent.desired.os_user}". Also delete that user account entirely (userdel -r)?\n\n` +
      `Type the user name to delete the account. Anything else leaves the account and only removes the service.`,
      agent.desired.os_user);
  }

  await streamInto(document.getElementById("inspect-decommission-output"), e.target,
    `/api/agents/${id}/decommission`, { purge, remove_user: removeUser, confirm: true });
  // The modal stays open: its output says whether the teardown worked, and the
  // operator has to be able to read it. They close it themselves.
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
    owner: f.owner.value.trim() || null,
    member_logs: f.member_logs.checked,
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
  f.member_logs.checked = !!a?.member_logs;
  f.notes.value = a?.notes ?? "";
  f.tags.value = (a?.tags ?? []).join(", ");
  f.templates.value = (a?.templates ?? []).join(", ");
  f.reach_endpoint.value = a?.reach?.endpoint ?? "";
  f.reach_network.value = a?.reach?.network ?? "";
  f.reach_bind.value = a?.reach?.bind ?? "";
  f.reach_board.checked = !!a?.reach?.board;
  f.reach_chat.checked = !!a?.reach?.chat;
  f.reach_chat_ids.value = (a?.reach?.chat_ids ?? []).join(", ");
  // The record as loaded: saving must keep what the form doesn't show
  // (reach, and whatever fields come later), not rebuild the agent from scratch.
  f.dataset.original = JSON.stringify(a ?? {});
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
  if (!confirm(`Delete Beacon's record of agent ${id}?\n\nThis does not touch the host: if the agent was deployed, its gateway keeps running with nothing tracking it. To stop and uninstall it, cancel and use Decommission instead.`)) return;
  try {
    await api.del("agents", id);
  } catch (err) {
    alert(err.message);
  }
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
  const original = JSON.parse(f.dataset.original || "{}");
  const endpoint = f.reach_endpoint.value.trim();
  const body = {
    ...original,
    id: f.id.value,
    type: f.type.value,
    host: f.host.value,
    profile: f.profile.value || null,
    owner: f.owner.value || null,
    notes: f.notes.value || null,
    tags: splitList(f.tags.value),
    templates: splitList(f.templates.value),
    desired,
    // Keeps what the form doesn't show (callers, capabilities); no endpoint means not in the mesh.
    reach: endpoint ? {
      ...(original.reach ?? {}),
      endpoint,
      network: f.reach_network.value.trim() || null,
      bind: f.reach_bind.value.trim() || null,
      board: f.reach_board.checked,
      chat: f.reach_chat.checked,
      chat_ids: f.reach_chat_ids.value.split(",").map((s) => s.trim()).filter(Boolean),
    } : null,
  };
  try {
    await api.put("agents", body.id, body);
    closeModal("agent-modal");
    renderAgents();
    renderTemplates();
    if (body.reach && !original.reach) openEnroll(body.id);  // a new member of the mesh: bring it in
  } catch (err) {
    alert(err.message);
  }
});

// ---- enrolling an existing agent ----
let enrollId = null;
let enrollTimer = null;

function openEnroll(id) {
  enrollId = id;
  document.getElementById("enroll-form").reset();
  document.getElementById("enroll-command").hidden = true;
  document.getElementById("enroll-title").textContent = `Bring ${id} into the mesh`;
  openModal("enroll-modal");
  clearInterval(enrollTimer);
  enrollTimer = setInterval(refreshEnroll, 5000);
  refreshEnroll();
}

async function refreshEnroll() {
  const modal = document.getElementById("enroll-modal");
  if (!modal.classList.contains("open")) { clearInterval(enrollTimer); return; }
  const el = document.getElementById("enroll-steps");
  try {
    const r = await json(`/api/agents/${encodeURIComponent(enrollId)}/onboarding`);
    el.innerHTML = r.steps.map((s) => finding(s.done ? "ok" : "info", `${esc(s.label)}${s.detail ? ` <span class="hint">— ${esc(s.detail)}</span>` : ""}`)).join("")
      + (r.done ? finding("ok", "<strong>Done.</strong> The agent is in the mesh.") : "");
  } catch (err) {
    el.innerHTML = finding("critical", esc(err.message));
  }
}

document.getElementById("enroll-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const docker = f.docker.value.trim();
  try {
    const r = await json(`/api/agents/${encodeURIComponent(enrollId)}/enroll`, "POST", {
      docker: docker || null, docker_user: f.docker_user.value.trim() || "hermes", schedule: f.schedule.checked, confirm: true,
    });
    document.getElementById("enroll-cmd").textContent = r.command;
    document.getElementById("enroll-who").textContent = docker ? " as root" : ", as the user that runs the agent";
    document.getElementById("enroll-expires").textContent = new Date(r.expires_at * 1000).toLocaleTimeString();
    document.getElementById("enroll-command").hidden = false;
    refreshEnroll();
  } catch (err) {
    alert(err.message);
  }
});

document.getElementById("enroll-copy").addEventListener("click", async (e) => {
  try {
    await navigator.clipboard.writeText(document.getElementById("enroll-cmd").textContent);
    e.target.textContent = "Copied";
  } catch {
    e.target.textContent = "Select and copy it by hand";
  }
});

// ---- board ----
const BOARD_STATES = ["todo", "doing", "blocked", "review", "done"];

// Who besides operators may see intake cards. Changed only from this page's session.
async function renderIntakeVisibility() {
  const box = document.getElementById("intake-visibility");
  try {
    const v = await json("/api/intake/visibility");
    document.getElementById("intake-see-members").checked = v.members;
    document.getElementById("intake-see-agents").checked = v.agents;
    box.hidden = false;
  } catch (err) {
    box.hidden = true;  // no visibility setting to show: the board still works
  }
}

async function changeIntakeVisibility() {
  const body = {
    members: document.getElementById("intake-see-members").checked,
    agents: document.getElementById("intake-see-agents").checked,
  };
  try {
    await json("/api/intake/visibility", "PUT", body);
  } catch (err) {
    alert(err.message);
  }
  renderIntakeVisibility();
}
document.getElementById("intake-see-members").addEventListener("change", changeIntakeVisibility);
document.getElementById("intake-see-agents").addEventListener("change", changeIntakeVisibility);

const ARCHIVE_AFTER_SECONDS = 48 * 3600;  // a done card stays in the Done column this long after its last change
const NONE = "__none";                    // the filter value for "no assignee" and "no project"
let boardCards = [], boardWaiting = {};

async function renderBoard() {
  renderIntakeVisibility();
  const el = document.getElementById("board-columns");
  let cards;
  try {
    cards = await json("/api/board?include_done=true");
  } catch (err) {
    el.innerHTML = `<p class="empty">Could not load the board: ${esc(err.message)}</p>`;
    return;
  }
  // Requests that wait for an answer, or for the requester after "changes requested".
  // The workflow list is already cut to what this viewer may see.
  const waiting = {};
  (await api.listWorkflows().catch(() => []))
    .filter((w) => w.state === "pending" || w.state === "changes_requested")
    .forEach((w) => { waiting[w.card_id] = (waiting[w.card_id] || 0) + 1; });
  boardCards = cards;
  boardWaiting = waiting;
  fillBoardFilters();
  drawBoard();
  // Who the viewer may give a card to: a member, their own agents and the ones
  // the policy opens to members; an operator, every agent in the mesh.
  json("/api/board/assignees").then((ids) => {
    assigneeIds = ids;
    document.querySelectorAll("select[data-assignees]").forEach((select) => setAssignee(select, select.value));
  }, () => {});
}

function boardCardHtml(c) {
  return `<button type="button" class="board-card" data-card="${esc(c.id)}">
        <span>#${esc(c.id)} ${esc(c.title)}</span>
        <span class="board-card-meta">${esc(c.assignee ?? "unassigned")}${c.project ? ` · ${esc(c.project)}` : ""}${
          c.help_by ? ' <span class="chip chip-warn">help wanted</span>' : ""}${
          c.stuck ? ' <span class="chip chip-warn" title="People were told it is not moving, and nothing has changed since">stuck</span>' : ""}${
          boardWaiting[c.id] ? ` <span class="chip chip-warn">${boardWaiting[c.id]} open request${boardWaiting[c.id] > 1 ? "s" : ""}</span>` : ""}${
          c.blocked_by_count ? ` <span class="chip chip-warn" title="unfinished dependencies">blocked by ${esc(c.blocked_by_count)}</span>` : ""}${
          c.state === "blocked" ? ` <span class="chip${c.blocked_reason === "cannot" ? " chip-warn" : ""}"${c.waiting_on ? ` title="${esc(c.waiting_on)}"` : ""}>${esc(c.blocked_reason ?? "no reason")}</span>` : ""}${
          c.state !== "done" ? c.help_by ? "" : deliveryChip(c.delivery) : ""}</span>
      </button>`;
}

// The filter's dropdowns list what the cards have. A choice that is no longer there falls back to "any".
function fillBoardFilters() {
  const fill = (id, label, none, values) => {
    const select = document.getElementById(id);
    const chosen = select.value;
    select.innerHTML = `<option value="">${label}</option><option value="${NONE}">${none}</option>${
      [...values].sort().map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("")}`;
    select.value = [...select.options].some((o) => o.value === chosen) ? chosen : "";
  };
  fill("board-filter-assignee", "any assignee", "unassigned", new Set(boardCards.map((c) => c.assignee).filter(Boolean)));
  fill("board-filter-project", "any project", "no project", new Set(boardCards.map((c) => c.project).filter(Boolean)));
}

function boardFilter() {
  return {
    terms: document.getElementById("board-search").value.toLowerCase().split(/\s+/).filter(Boolean),
    assignee: document.getElementById("board-filter-assignee").value,
    project: document.getElementById("board-filter-project").value,
    blocked: document.getElementById("board-filter-blocked").checked,
  };
}

// Every word typed must appear somewhere on the card: its number, title, text, agent, project, state, why
// it is blocked, or what it waits on. Cheap to do here: the board already holds every card.
function boardMatches(c, f) {
  if (f.assignee && (c.assignee ?? "") !== (f.assignee === NONE ? "" : f.assignee)) return false;
  if (f.project && (c.project ?? "") !== (f.project === NONE ? "" : f.project)) return false;
  if (f.blocked && c.state !== "blocked") return false;
  if (!f.terms.length) return true;
  const text = [`#${c.id}`, c.title, c.body, c.assignee, c.project, c.state, c.blocked_reason, c.waiting_on,
    c.next_action, c.done_when].filter(Boolean).join(" ").toLowerCase();
  return f.terms.every((t) => text.includes(t));
}

function drawBoard() {
  const f = boardFilter();
  const active = Boolean(f.terms.length || f.assignee || f.project || f.blocked);
  const now = Date.now() / 1000;
  const archived = (c) => c.state === "done" && now - c.updated_at >= ARCHIVE_AFTER_SECONDS;
  const shown = boardCards.filter((c) => boardMatches(c, f));
  const el = document.getElementById("board-columns");
  el.innerHTML = BOARD_STATES.map((state) => {
    const items = shown.filter((c) => c.state === state && !archived(c));
    return `<div class="board-col"><h2>${state} <span class="hint">${items.length}</span></h2>${items.map(boardCardHtml).join("")}</div>`;
  }).join("");

  const old = shown.filter(archived);
  const everOld = boardCards.filter(archived).length;
  const archive = document.getElementById("board-archive");
  archive.hidden = everOld === 0;
  document.getElementById("board-archive-count").textContent = active ? `${old.length} of ${everOld}` : `${everOld}`;
  document.getElementById("board-archive-list").innerHTML = old.length
    ? old.map(boardCardHtml).join("") : '<p class="hint">No archived card matches.</p>';
  if (active && old.length) archive.open = true;  // a search that finds something there should show it

  document.getElementById("board-filter-clear").hidden = !active;
  document.getElementById("board-filter-count").textContent = active ? `${shown.length} of ${boardCards.length} cards` : "";
  document.querySelectorAll("#board-columns [data-card], #board-archive-list [data-card]")
    .forEach((b) => b.addEventListener("click", () => openCard(b.dataset.card)));
}

["board-search", "board-filter-assignee", "board-filter-project", "board-filter-blocked"].forEach((id) =>
  document.getElementById(id).addEventListener(id === "board-search" ? "input" : "change", drawBoard));
document.getElementById("board-filter-clear").addEventListener("click", () => {
  document.getElementById("board-search").value = "";
  document.getElementById("board-filter-assignee").value = "";
  document.getElementById("board-filter-project").value = "";
  document.getElementById("board-filter-blocked").checked = false;
  drawBoard();
});

let assigneeIds = [];

// A dropdown of the agents to choose from. A card's current assignee that is not
// among them (a person, or an agent this viewer may not assign to) stays
// selectable, so saving the card never silently unassigns it.
function setAssignee(select, current) {
  const ids = current && !assigneeIds.includes(current) ? [...assigneeIds, current] : assigneeIds;
  select.innerHTML = `<option value="">unassigned</option>${ids.map((id) => `<option value="${esc(id)}">${esc(id)}</option>`).join("")}`;
  select.value = current || "";
}

async function openCard(id) {
  let c;
  try {
    c = await json(`/api/board/cards/${encodeURIComponent(id)}`);
  } catch (err) {
    alert(err.message);
    return;
  }
  const requests = await json(`/api/workflows?card_id=${encodeURIComponent(id)}`).catch(() => []);
  const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const unset = '<span class="hint">not set</span>';
  // Links are pointers the card's editors typed, not something Beacon checked.
  const links = c.links.map((l) => `<li><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${
    esc(l.label || l.url)}</a></li>`).join("");
  // References are pointers to work elsewhere. The state on one is a hint that whoever added it
  // typed, with its age: Beacon does not fetch or check it, so it is worded as a claim, not as state.
  const refs = (c.references || []).map((r) => {
    const wf = r.workflow_id ? requests.find((w) => w.id === r.workflow_id) : null;
    const hint = r.last_seen_state
      ? ` <span class="hint">· last seen “${esc(r.last_seen_state)}”, typed ${esc(when(r.last_seen_at))}; Beacon has not checked it</span>` : "";
    const request = r.workflow_id
      ? ` <span class="hint">· workflow #${esc(r.workflow_id)}${wf ? ` (${esc(wf.state)})` : ""}</span>` : "";
    return `<li><span class="chip">${esc(r.provider)}</span> ${esc(r.kind.replace(/_/g, " "))}
      <a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.label)}</a>
      <code>${esc(r.external_id)}</code>${hint}${request}</li>`;
  }).join("");
  // What a request says now, without opening it: who it waits on, until when, and its result.
  const resultOf = (r) => (r == null ? "" : typeof r === "string" ? r : r.text || r.option || r.note || "");
  // Related cards: each opens in place. Beacon computes "blocked by" from the dependencies and their
  // states each time; it is not something anyone typed.
  const related = (cards) => cards.map((r) => `<li><a href="#" data-open-card="${esc(r.id)}">#${esc(r.id)} ${esc(r.title)}</a>
    <span class="chip">${esc(r.state)}</span></li>`).join("");
  const reqs = requests.map((w) => {
    const waiting = w.state === "pending" ? ` · waiting for ${w.eligible_responder === "human" ? "an operator" : w.eligible_responder}, expires ${when(w.expires_at)}`
      : w.state === "changes_requested" ? ` · waiting for ${w.requester} to send a changed plan` : ` · responder ${w.eligible_responder}`;
    const result = resultOf(w.response);
    return `<li><span class="chip">${esc(w.state)}</span> ${esc(WORKFLOW_KIND_LABEL[w.kind] || w.kind)}: ${esc(w.title)}
      <span class="hint">${esc(waiting)}${result ? ` · ${w.state === "changes_requested" ? "asked for" : "answer"}: ${esc(result)}` : ""}</span></li>`;
  }).join("");
  document.getElementById("card-detail").innerHTML = `
    <h2>#${esc(c.id)} ${esc(c.title)}</h2>
    <p class="hint">${esc(c.state)} · ${esc(c.assignee ?? "unassigned")} · ${esc(c.project ?? "no project")} · created by ${esc(c.created_by)} · ${when(c.created_at)}</p>
    <dl class="card-plan">
      <dt>Next action</dt><dd>${c.next_action ? esc(c.next_action) : unset}</dd>
      <dt>Done when</dt><dd>${c.done_when ? esc(c.done_when) : unset}</dd>
      ${c.state === "blocked" ? `<dt>Blocked</dt><dd>${c.blocked_reason ? esc(c.blocked_reason) : '<span class="hint">no reason given (blocked before reasons existed)</span>'}${
        c.waiting_on ? ` · ${esc(c.waiting_on)}` : ""}</dd>` : ""}
      ${reqs ? `<dt>Requests</dt><dd><ul class="card-list">${reqs}</ul><span class="hint">Answer in the Workflows tab.</span></dd>` : ""}
      ${c.blocked_by.length ? `<dt>Blocked by</dt><dd><ul class="card-list">${related(c.blocked_by)}</ul></dd>` : ""}
      ${c.parent ? `<dt>Part of</dt><dd><ul class="card-list">${related([c.parent])}</ul></dd>` : ""}
      ${c.children.length ? `<dt>Child cards</dt><dd><ul class="card-list">${related(c.children)}</ul></dd>` : ""}
      ${c.blocks.length ? `<dt>Blocks</dt><dd><ul class="card-list">${related(c.blocks)}</ul></dd>` : ""}
      ${refs ? `<dt>References</dt><dd><ul class="card-list">${refs}</ul></dd>` : ""}
      ${links ? `<dt>Links</dt><dd><ul class="card-list">${links}</ul></dd>` : ""}
    </dl>
    ${c.body ? `<p class="card-body">${esc(c.body)}</p>` : ""}
    <h3 class="card-section">History <span class="hint">${c.events.length}</span></h3>
    <ol class="card-history">${c.events.map((e) => cardEvent(e, when)).join("")}</ol>`;
  document.querySelectorAll("#card-detail [data-open-card]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    openCard(a.dataset.openCard);
  }));
  const form = document.getElementById("card-update-form");
  form.dataset.card = c.id;
  form.state.value = c.state;
  setAssignee(form.assignee, c.assignee);
  form.next_action.value = c.next_action ?? "";
  form.done_when.value = c.done_when ?? "";
  form.blocked_reason.value = c.blocked_reason ?? "";
  form.waiting_on.value = c.waiting_on ?? "";
  showBlockedFields(form);
  form.links.value = c.links.map((l) => `${l.url} ${l.label}`.trim()).join("\n");
  form.note.value = "";
  // Milestones come from the card's project; help is asked for an unassigned open card in one.
  const projectRef = c.project ? (await json("/api/projects?include_closed=true").catch(() => [])).find((p) => p.name === c.project) : null;
  const milestones = projectRef ? (await json(`/api/projects/${projectRef.id}`).catch(() => ({ milestones: [] }))).milestones : [];
  const open = milestones.filter((m) => m.state === "open" || m.id === c.milestone_id);
  document.getElementById("card-milestone").hidden = !open.length;
  form.milestone.innerHTML = `<option value="0">no milestone</option>${open.map((m) => `<option value="${esc(m.id)}">${esc(m.title)}</option>`).join("")}`;
  form.milestone.value = String(c.milestone_id ?? 0);
  document.getElementById("card-help").hidden = !(c.help_by || (projectRef?.state === "active" && !c.assignee && c.state !== "done"));
  form.help_wanted.checked = !!c.help_by;
  const roles = document.getElementById("card-help").hidden ? [] : await json("/api/roles").catch(() => []);
  form.help_roles.innerHTML = roles.map((r) => `<option value="${esc(r.role)}"${c.help_roles.includes(r.role) ? " selected" : ""}${
    r.agents.length ? "" : " disabled"}>${esc(r.role)}: ${esc(r.description)}${r.agents.length ? "" : " (nobody you may ask)"}</option>`).join("");
  form.help_roles.disabled = !!c.help_by;  // an open call keeps the classes it was made with
  document.getElementById("card-help-roles").hidden = !roles.length;
  form.dataset.milestone = String(c.milestone_id ?? 0);
  form.dataset.help = c.help_by ? "1" : "";
  openModal("card-modal");
  // Where the history scrolls on its own (wide screens), start at the newest entry.
  const history = document.querySelector("#card-detail .card-history");
  history.scrollTop = history.scrollHeight;
}

// Where the card's latest notice is: still waiting for the agent's sync, being
// worked on, answered, or lost. While testing, this is what tells "has not
// synced yet" from "is on it" from "something is wrong".
const NOTICE_WORDS = {
  queued: (to) => `waiting for ${to}'s sync`, delivering: (to) => `delivering to ${to}`,
  working: (to) => `${to} is on it`, delivered: (to) => `${to} has it`,
  failed: () => "notice failed", expired: () => "notice expired", uncertain: (to) => `${to} has it`,
};

function deliveryChip(d) {
  if (!d) return "";
  const words = d.answered_at ? `${d.to} answered` : (NOTICE_WORDS[d.state] || (() => d.state))(d.to);
  const warn = d.state === "failed" || d.state === "expired";
  return ` <span class="chip${warn ? " chip-warn" : ""}"${d.error ? ` title="${esc(d.error)}"` : ""}>${esc(words)}</span>`;
}

// Comments and answers are what people and agents wrote, so they read as text;
// moves, edits, notices and reminders are Beacon's record and stay one quiet line.
function cardEvent(e, when) {
  const comment = e.kind === "comment" || e.kind === "answer";
  const notice = e.notice && (NOTICE_WORDS[e.notice.state] || (() => e.notice.state))(e.notice.to);
  return `<li class="${comment ? "card-comment" : "card-event"}">
    <div class="card-event-head"><strong>${esc(e.author)}</strong>${e.kind === "comment" ? "" : `<span>${esc(e.kind.replace("_", " "))}</span>`}<time>${
      when(e.created_at)}</time>${e.notice ? `<span class="chip${["failed", "expired"].includes(e.notice.state) ? " chip-warn" : ""}"${
        e.notice.last_error ? ` title="${esc(e.notice.last_error)}"` : ""}>${esc(notice)}</span>` : ""}${
      e.context_id ? `<code title="A2A context">${esc(e.context_id)}</code>` : ""}</div>
    <div class="card-text">${esc(e.body)}</div></li>`;
}

// One link per line: the URL, then the rest of the line as its label.
function parseLinks(text) {
  return text.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    const [url, ...label] = line.split(/\s+/);
    return { url, label: label.join(" ") };
  });
}

function showBlockedFields(form) {
  document.getElementById("blocked-fields").hidden = form.state.value !== "blocked";
}
document.getElementById("card-update-form").state.addEventListener("change", (e) => showBlockedFields(e.target.form));

document.getElementById("card-update-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await json(`/api/board/cards/${encodeURIComponent(f.dataset.card)}`, "PATCH",
      { state: f.state.value, assignee: f.assignee.value.trim(), next_action: f.next_action.value,
        done_when: f.done_when.value, links: parseLinks(f.links.value), note: f.note.value.trim() || null,
        // Only a blocked card has a reason; the server refuses a move to blocked without one.
        blocked_reason: f.state.value === "blocked" ? f.blocked_reason.value || null : null,
        waiting_on: f.state.value === "blocked" ? f.waiting_on.value.trim() || null : null,
        // Only when changed: asking for help again, or re-pointing, is an action of its own.
        milestone: f.milestone.value !== f.dataset.milestone ? Number(f.milestone.value) : null,
        help_wanted: f.help_wanted.checked !== !!f.dataset.help ? f.help_wanted.checked : null,
        help_roles: f.help_wanted.checked && !f.dataset.help ? [...f.help_roles.selectedOptions].map((o) => o.value) : null });
    closeModal("card-modal");
    renderBoard();
  } catch (err) {
    alert(err.message);
  }
});

document.getElementById("refresh-board").addEventListener("click", renderBoard);
document.getElementById("add-card").addEventListener("click", async () => {
  document.getElementById("card-create-form").reset();
  const open = await json("/api/projects").catch(() => []);
  document.querySelector("[data-projects]").innerHTML = `<option value="">no project</option>${
    open.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join("")}`;
  openModal("card-create-modal");
});

document.getElementById("card-create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await json("/api/board/cards", "POST", {
      title: f.title.value, body: f.body.value,
      project: f.project.value || null, assignee: f.assignee.value.trim() || null,
      next_action: f.next_action.value || null, done_when: f.done_when.value || null,
    });
    closeModal("card-create-modal");
    renderBoard();
  } catch (err) {
    alert(err.message);
  }
});

// ---- projects ----
// The server says what the viewer may do with each (`can`); the page only offers that, and the
// server refuses the rest anyway (backend/projects.py).
let openedProject = null;

const projectOwner = (p) => p.owner ?? "the operators";

async function renderProjects() {
  const el = document.getElementById("project-list");
  let items;
  try {
    items = await json(`/api/projects?include_closed=${document.getElementById("projects-closed").checked}`);
  } catch (err) {
    el.innerHTML = `<p class="empty">Could not load projects: ${esc(err.message)}</p>`;
    return;
  }
  document.getElementById("projects-empty").hidden = items.length > 0;
  el.innerHTML = items.map((p) => {
    const total = Object.values(p.cards).reduce((a, b) => a + b, 0);
    const counts = BOARD_STATES.filter((s) => p.cards[s]).map((s) => `${p.cards[s]} ${s}`).join(", ");
    return `<article class="workflow-item">
      <header class="wf-head">
        <div><div class="wf-kind">#${esc(p.id)} · ${esc(projectOwner(p))}</div>
          <h3 class="wf-title"><a href="#" data-open-project="${esc(p.id)}">${esc(p.name)}</a></h3></div>
        <span class="chip${p.state === "paused" ? " chip-warn" : ""}">${esc(p.state)}</span>
      </header>
      <p>${p.goal ? esc(p.goal) : '<span class="hint">No goal set.</span>'}</p>
      ${p.status ? `<p class="hint">${esc(p.status)}</p>` : ""}
      <p class="wf-from">${total ? `Cards: ${esc(counts)}` : "No cards."}</p>
    </article>`;
  }).join("");
  el.querySelectorAll("[data-open-project]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    openProject(a.dataset.openProject);
  }));
}

async function openProject(id) {
  let p;
  try {
    p = await json(`/api/projects/${encodeURIComponent(id)}`);
  } catch (err) {
    alert(err.message);
    return;
  }
  openedProject = p;
  const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const links = p.links.map((l) => `<li><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.label || l.url)}</a></li>`).join("");
  const cards = p.card_list.map((c) => `<li><a href="#" data-open-card="${esc(c.id)}">#${esc(c.id)} ${esc(c.title)}</a>
    <span class="chip">${esc(c.state)}</span> <span class="hint">${esc(c.assignee ?? "unassigned")}</span></li>`).join("");
  document.getElementById("project-detail").innerHTML = `
    <h2>${esc(p.name)}</h2>
    <p class="hint">#${esc(p.id)} · ${esc(p.state)} · owner ${esc(projectOwner(p))} · started by ${esc(p.created_by)} · ${when(p.created_at)}</p>
    <dl class="card-plan">
      <dt>Goal</dt><dd>${p.goal ? esc(p.goal) : '<span class="hint">not set</span>'}${p.goal_by ? ` <span class="hint">(set by ${esc(p.goal_by)})</span>` : ""}</dd>
      <dt>Done when</dt><dd>${p.done_when ? esc(p.done_when) : '<span class="hint">not set</span>'}</dd>
      <dt>Status</dt><dd>${p.status ? `${esc(p.status)} <span class="hint">(${when(p.status_at)})</span>` : '<span class="hint">none yet</span>'}</dd>
      ${links ? `<dt>Links</dt><dd><ul class="card-list">${links}</ul></dd>` : ""}
      <dt>Milestones</dt><dd>${milestoneList(p, when)}</dd>
      <dt>Cards</dt><dd>${cards ? `<ul class="card-list">${cards}</ul>` : '<span class="hint">none</span>'}</dd>
      ${p.evidence ? `<dt>Evidence</dt><dd>${evidenceHtml(p.evidence)} <span class="hint">(claimed; Beacon has not checked it)</span></dd>` : ""}
    </dl>
    <h3 class="card-section">History <span class="hint">${p.events.length}</span></h3>
    <ol class="card-history">${p.events.map((e) => cardEvent(e, when)).join("")}</ol>`;
  document.querySelectorAll("#project-detail [data-open-card]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    openCard(a.dataset.openCard);
  }));
  document.querySelectorAll("#project-detail [data-ms]").forEach((b) => b.addEventListener("click", () =>
    changeMilestone(Number(b.dataset.id), b.dataset.ms)));
  const form = document.getElementById("project-form");
  document.getElementById("milestone-add").hidden = !p.can.steer || p.state === "done" || p.state === "abandoned";
  form.ms_title.value = form.ms_done_when.value = form.ms_due.value = "";
  form.status.value = p.status ?? "";
  form.goal.value = p.goal;
  form.done_when.value = p.done_when ?? "";
  form.owner.value = p.owner ?? "";
  form.note.value = "";
  form.status.disabled = form.owner.disabled = !p.can.steer;
  form.goal.disabled = form.done_when.disabled = !p.can.goal;
  document.getElementById("project-edit").hidden = !p.can.steer && !p.can.goal;
  form.querySelector('[type="submit"]').hidden = !p.can.steer && !p.can.goal;
  const closed = p.state === "done" || p.state === "abandoned";
  const offered = { paused: p.state === "active", active: p.state !== "active", done: !closed, abandoned: !closed };
  form.querySelectorAll("[data-project-state]").forEach((b) => {
    b.hidden = !p.can.steer || !offered[b.dataset.projectState];
    if (b.dataset.projectState === "active") b.textContent = closed ? "Reopen" : "Resume";
  });
  openModal("project-modal");
}

function evidenceHtml(e) {
  if (!e.links.length) return `none: ${esc(e.none_because ?? "")}`;
  return e.links.map((l) => `<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.label || l.url)}</a>`).join(", ");
}

function milestoneList(p, when) {
  if (!p.milestones.length) return '<span class="hint">none</span>';
  const closed = p.state === "done" || p.state === "abandoned";
  const button = (m, action, label) => `<button type="button" class="link-btn" data-ms="${action}" data-id="${esc(m.id)}">${label}</button>`;
  return `<ul class="card-list">${p.milestones.map((m) => {
    const n = p.card_list.filter((c) => c.milestone_id === m.id).length;
    return `<li><span class="chip">${esc(m.state)}</span> ${esc(m.title)} <span class="hint">done when: ${esc(m.done_when)}${
      m.due ? ` · ${esc(m.due)}` : ""} · ${n} card${n === 1 ? "" : "s"} · set by ${esc(m.set_by)}</span>${
      m.evidence ? `<br><span class="hint">closed by ${esc(m.closed_by)}, ${when(m.closed_at)}:</span> ${evidenceHtml(m.evidence)}` : ""}${
      closed || !p.can.steer ? "" : ` ${m.state === "open" ? button(m, "done", "Close") : button(m, "open", "Reopen")}`}${
      !closed && m.can_change ? ` ${button(m, "remove", "Remove")}` : ""}</li>`;
  }).join("")}</ul>`;
}

// Closing something takes a link to follow, or the reason there is none. One question for both.
function askEvidence(what) {
  const text = prompt(`Evidence that ${what} is done: link(s) to the ticket, pull request or commit.\nIf there is none, say why.`);
  if (!text?.trim()) return null;
  const words = text.trim().split(/\s+/);
  return words.every((w) => /^https?:\/\//.test(w))
    ? { evidence_links: words.map((url) => ({ url, label: "" })) } : { no_evidence_because: text.trim() };
}

async function changeMilestone(id, action) {
  const p = openedProject;
  let body = action === "remove" ? { remove: true } : { state: action };
  if (action === "remove" && !confirm("Remove this milestone? Its cards stay in the project.")) return;
  if (action === "done") {
    const evidence = askEvidence("this milestone");
    if (!evidence) return;
    body = { ...body, ...evidence };
  }
  try {
    await json(`/api/projects/${p.id}/milestones/${id}`, "PATCH", body);
    await openProject(p.id);
  } catch (err) {
    alert(err.message);
  }
}

document.getElementById("milestone-add-button").addEventListener("click", async () => {
  const f = document.getElementById("project-form");
  try {
    await json(`/api/projects/${openedProject.id}/milestones`, "POST",
      { title: f.ms_title.value, done_when: f.ms_done_when.value, due: f.ms_due.value || null });
    await openProject(openedProject.id);
  } catch (err) {
    alert(err.message);
  }
});

// Only what changed is sent: sending an untouched goal would read as an attempt to set it.
async function changeProject(extra) {
  const f = document.getElementById("project-form"), p = openedProject;
  const body = { ...extra };
  if (p.can.steer && f.status.value.trim() !== (p.status ?? "")) body.status = f.status.value;
  if (p.can.steer && f.owner.value.trim() !== (p.owner ?? "")) body.owner = f.owner.value.trim();
  if (p.can.goal && f.goal.value.trim() !== p.goal) body.goal = f.goal.value;
  if (p.can.goal && f.done_when.value.trim() !== (p.done_when ?? "")) body.done_when = f.done_when.value;
  if (f.note.value.trim()) body.note = f.note.value.trim();
  try {
    await json(`/api/projects/${p.id}`, "PATCH", body);
    await openProject(p.id);
    renderProjects();
  } catch (err) {
    alert(err.message);
  }
}

document.getElementById("project-form").addEventListener("submit", (e) => { e.preventDefault(); changeProject({}); });
document.querySelectorAll("[data-project-state]").forEach((b) => b.addEventListener("click", () => {
  const state = b.dataset.projectState;
  if (state === "abandoned" && !confirm(`Abandon ${openedProject.name}? Agents on it are told to stop.`)) return;
  const evidence = state === "done" ? askEvidence(openedProject.name) : {};
  if (evidence) changeProject({ state, ...evidence });
}));
document.querySelector("[data-project-comment]").addEventListener("click", async () => {
  const note = document.getElementById("project-form").note.value.trim();
  if (!note) return;
  try {
    await json(`/api/projects/${openedProject.id}/comments`, "POST", { text: note });
    await openProject(openedProject.id);
  } catch (err) {
    alert(err.message);
  }
});
document.getElementById("refresh-projects").addEventListener("click", renderProjects);
document.getElementById("projects-closed").addEventListener("change", renderProjects);
document.querySelector('[data-tab="projects"]').addEventListener("click", renderProjects);
document.getElementById("add-project").addEventListener("click", async () => {
  document.getElementById("project-create-form").reset();
  const ids = await json("/api/board/assignees").catch(() => []);
  document.getElementById("project-owners").innerHTML = ids.map((id) => `<option value="${esc(id)}">`).join("");
  openModal("project-create-modal");
});
document.getElementById("project-create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { name: f.name.value, goal: f.goal.value, done_when: f.done_when.value, links: parseLinks(f.links.value) };
  if (f.owner.value.trim()) body.owner = f.owner.value.trim();
  try {
    try {
      await json("/api/projects", "POST", body);
    } catch (err) {
      // A person decides whether a similar project is the same one.
      if (!err.message.includes("similar open projects") || !confirm(`${err.message}\n\nCreate it anyway?`)) throw err;
      await json("/api/projects", "POST", { ...body, confirm_new: true });
    }
    closeModal("project-create-modal");
    renderProjects();
  } catch (err) {
    alert(err.message);
  }
});

// ---- workflows ----
function workflowWhen(t) {
  return t ? new Date(t * 1000).toLocaleString() : "-";
}

const WORKFLOW_KIND_LABEL = { decision: "Decision", question: "Question", review: "Review", approval: "Approval", handoff: "Handoff" };

// Summaries are Markdown written by agents, so they are escaped before any markup
// is added. Only a small subset is drawn: headings, lists, bold, code and code blocks.
// No links and no images: a link from an agent is text, not something to follow.
function renderMarkdown(source) {
  const out = [];
  let para = [], list = null, code = null;
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const flush = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of String(source ?? "").replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    if (code !== null) {
      if (line.trim().startsWith("```")) { out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`); code = null; }
      else code.push(raw);
      continue;
    }
    if (line.trim().startsWith("```")) { flush(); closeList(); code = []; continue; }
    if (!line.trim()) { flush(); closeList(); continue; }
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (heading) { flush(); closeList(); out.push(`<h4>${inline(heading[1])}</h4>`); }
    else if (bullet || numbered) {
      flush();
      const tag = bullet ? "ul" : "ol";
      if (list !== tag) { closeList(); out.push(`<${tag}>`); list = tag; }
      out.push(`<li>${inline((bullet || numbered)[1])}</li>`);
    } else { closeList(); para.push(line.trim()); }
  }
  if (code !== null) out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
  flush(); closeList();
  return out.join("");
}

// One card per request, in the order a reader needs it: what is asked, the plan,
// the answer box or choice, then the technical details the approver signs against.
// The badge counts requests waiting for an answer from this viewer: pending ones.
function setWorkflowBadge(items) {
  const badge = document.getElementById("workflows-badge");
  const open = items.filter((w) => w.state === "pending").length;
  badge.textContent = String(open);
  badge.hidden = open === 0;
}

async function updateWorkflowBadge() {
  try {
    setWorkflowBadge(await api.listWorkflows());
  } catch (err) {
    // no badge when the list cannot be read; the page itself shows the error
  }
}

let workflowCards = {};
// The requests as they are on screen, by id. An answer must name the plan the person read, not the
// one Beacon has by the time they click: the requester may have updated it since.
let shownWorkflows = new Map();

// What waits for an answer comes first, the soonest to expire at the very top; then what waits for its
// requester, then what is approved and not yet done; the rest by recency, as the server sends them.
const WORKFLOW_RANK = { pending: 0, changes_requested: 1, approved: 2, claimed: 2 };
function sortWorkflows(items) {
  return [...items].sort((a, b) => {
    const ra = WORKFLOW_RANK[a.state] ?? 3, rb = WORKFLOW_RANK[b.state] ?? 3;
    if (ra !== rb) return ra - rb;
    return ra === 0 ? !!b.urgent_reason - !!a.urgent_reason || a.expires_at - b.expires_at : 0;
  });
}

function renderWorkflowItems(items) {
  const list = document.getElementById("workflow-list");
  document.getElementById("workflows-empty").hidden = items.length > 0;
  setWorkflowBadge(items);
  items = sortWorkflows(items);
  shownWorkflows = new Map(items.map((w) => [String(w.id), w]));
  list.innerHTML = items.map((w) => {
    const card = workflowCards[w.card_id];
    // Where to look, from the card: what an agent pointed it at, outside Beacon. The state on a reference is
    // what whoever added it typed, so it is worded as a claim. The request's own text is never linked.
    const sources = card ? [
      ...(card.references || []).map((r) => `<li><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer"><span class="chip">${esc(r.provider)}</span>
        ${esc(r.kind.replace(/_/g, " "))} ${esc(r.label)} <code>${esc(r.external_id)}</code></a>${
        r.last_seen_state ? ` <span class="hint">last seen “${esc(r.last_seen_state)}”, typed ${esc(workflowWhen(r.last_seen_at))}; not checked by Beacon</span>` : ""}</li>`),
      ...(card.links || []).map((l) => `<li><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.label || l.url)}</a></li>`),
    ] : [];
    const aboutCard = card ? `<details class="wf-card-more"><summary>About this card</summary>
        <dl class="card-plan">
          <dt>State</dt><dd>${esc(card.state)} · ${esc(card.assignee ?? "unassigned")}${card.project ? ` · ${esc(card.project)}` : ""} · made by ${esc(card.created_by)}</dd>
          ${card.next_action ? `<dt>Next action</dt><dd>${esc(card.next_action)}</dd>` : ""}
          ${card.done_when ? `<dt>Done when</dt><dd>${esc(card.done_when)}</dd>` : ""}
        </dl>
        ${card.body ? `<p class="card-body">${esc(card.body)}</p>` : '<p class="hint">The card has no description.</p>'}
      </details>` : "";
    const pending = w.state === "pending";
    const isQuestion = w.kind === "question";
    const isDecision = w.kind === "decision";
    const id = esc(w.id);
    const buttons = !pending ? "" : isQuestion ? [
      `<button class="btn primary" data-workflow-action="answered" data-workflow-id="${id}">Send answer</button>`,
      `<button class="btn danger" data-workflow-action="rejected" data-workflow-id="${id}">Decline</button>`,
    ].join("") : [
      isDecision
        ? `<button class="btn primary" data-workflow-action="choose_option" data-workflow-id="${id}">Choose this option</button>`
        : `<button class="btn primary" data-workflow-action="approved" data-workflow-id="${id}">${w.kind === "handoff" ? "Accept handoff" : "Approve"}</button>`,
      `<button class="btn" data-workflow-action="changes_requested" data-workflow-id="${id}">Request changes</button>`,
      `<button class="btn danger" data-workflow-action="rejected" data-workflow-id="${id}">${w.kind === "handoff" ? "Decline" : "Reject"}</button>`,
    ].join("");
    // Options are the labels the plan explains; the first one starts selected.
    const choice = isDecision && pending ? `<fieldset class="wf-choice"><legend>Pick one</legend>${
      w.options.map((o, i) => `<label><input type="radio" name="wf-option-${id}" data-workflow-option="${id}" value="${esc(o)}"${i === 0 ? " checked" : ""}> ${esc(o)}</label>`).join("")
    }</fieldset>` : "";
    const answerBox = isQuestion && pending
      ? `<label class="wf-answer">Your answer <textarea rows="3" data-workflow-answer="${id}"></textarea></label>` : "";
    const answered = w.state === "answered" && w.response?.text
      ? `<div class="wf-note"><strong>Answer</strong>${renderMarkdown(w.response.text)}</div>` : "";
    const note = w.state === "changes_requested" && w.response?.note
      ? `<div class="wf-note"><strong>Changes requested</strong>${renderMarkdown(w.response.note)}</div>` : "";
    const waiting = w.state === "changes_requested"
      ? '<p class="hint">Waiting for the requester to send a changed plan.</p>' : "";
    const responder = w.eligible_responder === "human" ? "any operator"
      : w.eligible_responder.startsWith("role:") ? `any ${w.eligible_responder.slice(5)} agent (or an operator)` : w.eligible_responder;
    // Whether the requester asked a peer before asking a person: its own account, shown so the
    // person can judge the question. A request that did neither is flagged.
    const consulted = (w.consulted || []).length
      ? `<div class="wf-consulted"><strong>Consulted first</strong><ul>${w.consulted.map((c) =>
        `<li><strong>${esc(c.agent)}</strong>: ${esc(c.said)}${c.context_id ? ` <code>${esc(c.context_id)}</code>` : ""}</li>`).join("")}</ul></div>`
      : w.consult_skipped ? `<p class="hint">No peer consulted: ${esc(w.consult_skipped)}</p>`
      : w.consult_flag ? '<p class="wf-flag">No peer was consulted before asking.</p>' : "";
    return `<article class="workflow-item">
      <header class="wf-head">
        <div>
          <div class="wf-kind">#${esc(w.id)} · ${esc(WORKFLOW_KIND_LABEL[w.kind] || w.kind)} · CL${esc(w.clearance)}${w.urgent_reason ? ' · <strong class="wf-urgent">URGENT</strong>' : ""}</div>
          <h3 class="wf-title">${esc(w.title)}</h3>
        </div>
        <span class="chip">${esc(w.state)}</span>
      </header>
      <p class="wf-from">From <strong>${esc(w.requester)}</strong> to <strong>${esc(responder)}</strong> · expires ${esc(workflowWhen(w.expires_at))}</p>
      ${w.replaces_id ? `<details class="wf-card-more" data-replaces="${esc(w.replaces_id)}"><summary>Replaces #${esc(w.replaces_id)}: what it said</summary><div></div></details>` : ""}
      <p class="wf-card">On card <a href="#" data-open-card="${esc(w.card_id)}">#${esc(w.card_id)}${card ? ` ${esc(card.title)}` : ""}</a>${
        card ? ` <span class="chip">${esc(card.state)}</span>` : ""}</p>
      ${card ? (sources.length ? `<ul class="wf-sources">${sources.join("")}</ul>`
        : '<p class="hint">The card links to nothing outside Beacon. Ask the requester to add the PR or ticket to it.</p>') : ""}
      ${aboutCard}
      ${w.urgent_reason ? `<p class="wf-flag">Urgent: ${esc(w.urgent_reason)}</p>` : ""}
      <div class="wf-plan">${renderMarkdown(w.summary)}</div>
      ${consulted}
      ${w.answered_by ? `<p class="hint">Answered by <strong>${esc(w.answered_by)}</strong>, an AI client, for ${esc(w.on_behalf_of)}.</p>` : ""}
      ${w.require_human ? `<p class="wf-flag">Needs a person: ${esc(w.escalated_reason || "")} <span class="hint">(sent by ${esc(w.escalated_by || "a reviewer")})</span></p>` : ""}
      ${note}${answered}${waiting}
      ${choice}${answerBox}
      ${buttons ? `<div class="workflow-actions">${buttons}</div>` : ""}
      <details class="wf-tech"><summary>Technical details</summary>
        <div class="workflow-binding"><span class="hint">Plan revision and hash the answer must name</span><code>${esc(w.plan_revision)}</code><code>${esc(w.plan_hash)}</code></div>
        <pre>${esc(JSON.stringify(w.target, null, 2))}</pre>
      </details>
    </article>`;
  }).join("");
  list.querySelectorAll("[data-workflow-action]").forEach((button) => button.addEventListener("click", () => respondWorkflow(button)));
  list.querySelectorAll("[data-replaces]").forEach((d) => d.addEventListener("toggle", async () => {
    const box = d.querySelector("div");
    if (!d.open || box.innerHTML) return;
    try {
      const old = await json(`/api/workflows/${d.dataset.replaces}`);
      const approved = (old.events || []).some((e) => e.kind === "responded" && e.to_state === "approved");
      box.innerHTML = `<p class="hint">${approved ? "It had been approved. " : ""}Now ${esc(old.state)}.</p>
        <p><strong>${esc(old.title)}</strong></p>${renderMarkdown(old.summary)}`;
    } catch (err) {
      box.textContent = err.message;
    }
  }));
  list.querySelectorAll("[data-open-card]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    openCard(a.dataset.openCard);  // the card opens over this page: nothing to navigate back from
  }));
}

async function renderWorkflows() {
  const list = document.getElementById("workflow-list");
  list.innerHTML = '<p class="hint">Loading…</p>';
  try {
    // The cards the requests are about, so each shows its title and opens in place. A request still
    // lists without them if the board cannot be read.
    workflowCards = {};
    (await json("/api/board?include_done=true").catch(() => [])).forEach((c) => { workflowCards[c.id] = c; });
    renderWorkflowItems(await api.listWorkflows());
  } catch (err) {
    list.innerHTML = `<p class="finding critical"><span class="sev"></span><span class="summary">${esc(err.message)}</span></p>`;
  }
}

async function respondWorkflow(button) {
  const id = button.dataset.workflowId;
  const item = shownWorkflows.get(String(id));
  if (!item) return;
  const action = button.dataset.workflowAction;
  let response = {};
  if (action === "changes_requested") response = { note: prompt("What needs to change?", "") || "" };
  if (action === "rejected" && !confirm(`${item.kind === "question" ? "Decline" : "Reject"} “${item.title}”?`)) return;
  if (action === "answered") {
    const text = document.querySelector(`[data-workflow-answer="${CSS.escape(id)}"]`)?.value.trim();
    if (!text) return alert("Write an answer first.");
    response = { text };
  }
  if (action === "approved" || action === "choose_option") {
    if (!confirm(`${action === "choose_option" ? "Choose an option for" : item.kind === "handoff" ? "Accept the handoff:" : "Approve"} “${item.title}”?`)) return;
  }
  if (action === "choose_option" && item.kind === "decision") {
    response.option = document.querySelector(`input[data-workflow-option="${CSS.escape(id)}"]:checked`)?.value;
  }
  button.disabled = true;
  try {
    await api.respondWorkflow(id, { action, response, plan_revision: item.plan_revision, plan_hash: item.plan_hash });
    await renderWorkflows();
  } catch (err) {
    alert(err.message);
    await renderWorkflows();  // the plan may have changed: show what there is to answer now
  }
}

document.getElementById("workflows-refresh").addEventListener("click", renderWorkflows);
document.querySelector('[data-tab="workflows"]').addEventListener("click", renderWorkflows);

// ---- AI clients ----
// An operator's own AI client is an agent of its own (backend/clients.py). Making a client or a token
// is CL4: it asks for confirmation, then is sent with confirm=true. The token comes back once, here.
async function renderClients() {
  const body = document.getElementById("clients-body");
  let list;
  try {
    list = await json("/api/clients");
  } catch (err) {
    document.getElementById("clients-panel").hidden = true;  // not an operator, or not signed in: nothing to show
    return;
  }
  document.getElementById("clients-panel").hidden = false;
  document.getElementById("clients-empty").hidden = list.length > 0;
  const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never");
  body.innerHTML = list.map((c) => {
    const live = c.tokens.filter((t) => !t.revoked_at);
    const tokens = live.length
      ? live.map((t) => `<div>#${esc(t.id)} ${esc(t.label || "no label")} <span class="hint">· made ${esc(when(t.created_at))} by ${esc(t.created_by)} · last used ${esc(when(t.last_used_at))}</span>
          <button class="link-btn danger" data-client-revoke="${esc(c.id)}" data-token="${esc(t.id)}">Revoke</button></div>`).join("")
      : '<span class="hint">no live token</span>';
    return `<tr>
      <td><strong>${esc(c.id)}</strong></td>
      <td>${esc(c.acts_for ?? "-")}</td>
      <td>CL${esc(c.max_clearance)}</td>
      <td>${c.enabled ? '<span class="chip">on</span>' : '<span class="chip chip-warn">off</span>'}</td>
      <td>${tokens}</td>
      <td class="row-actions">
        <button class="link-btn" data-client-token="${esc(c.id)}">New token</button>
        <button class="link-btn" data-client-rotate="${esc(c.id)}">Rotate</button>
        <button class="link-btn" data-client-toggle="${esc(c.id)}" data-on="${c.enabled ? "1" : ""}">${c.enabled ? "Turn off" : "Turn on"}</button>
      </td></tr>`;
  }).join("");
  body.querySelectorAll("[data-client-token]").forEach((b) => b.addEventListener("click", () => mintClientToken(b.dataset.clientToken, false)));
  body.querySelectorAll("[data-client-rotate]").forEach((b) => b.addEventListener("click", () => mintClientToken(b.dataset.clientRotate, true)));
  body.querySelectorAll("[data-client-revoke]").forEach((b) => b.addEventListener("click", () => revokeClientToken(b.dataset.clientRevoke, b.dataset.token)));
  body.querySelectorAll("[data-client-toggle]").forEach((b) => b.addEventListener("click", () => toggleClient(b.dataset.clientToggle, b.dataset.on === "1")));
}

// Devices logged in through the native app (backend/app_devices.py). Shown only when there are
// some: most people never install the app, and an empty table would only ask why.
async function renderDevices() {
  let list;
  try {
    list = await json("/api/app/devices");
  } catch (err) {
    list = [];
  }
  document.getElementById("devices-panel").hidden = list.length === 0;
  const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const body = document.getElementById("devices-body");
  body.innerHTML = list.map((d) => `<tr>
      <td><strong>${esc(d.name)}</strong></td>
      <td>${esc(d.email)}</td>
      <td>${esc(when(d.created_at))}</td>
      <td>${esc(when(d.last_seen_at))}</td>
      <td class="row-actions"><button class="link-btn danger" data-device="${esc(d.id)}" data-name="${esc(d.name)}">Log out</button></td>
    </tr>`).join("");
  body.querySelectorAll("[data-device]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Log out ${b.dataset.name}? The app on it stops working until someone logs in again.`)) return;
    try {
      await json(`/api/app/devices/${encodeURIComponent(b.dataset.device)}`, "DELETE");
      renderDevices();
    } catch (err) {
      alert(err.message);
    }
  }));
}

function showClientToken(title, settings) {
  document.getElementById("client-token-title").textContent = title;
  document.getElementById("client-token-settings").textContent = settings;
  openModal("client-token-modal");
}

async function mintClientToken(id, rotate) {
  if (!confirm(rotate ? `Make a new token for ${id} and revoke all its other tokens?` : `Make a new token for ${id}?`)) return;
  try {
    const r = await json(`/api/clients/${encodeURIComponent(id)}/tokens`, "POST", { revoke_others: rotate, confirm: true });
    showClientToken(`New token for ${id}`, r.settings);
    renderClients();
  } catch (err) {
    alert(err.message);
  }
}

async function revokeClientToken(id, token) {
  if (!confirm(`Revoke token #${token} of ${id}? Whatever uses it stops working at once.`)) return;
  try {
    await json(`/api/clients/${encodeURIComponent(id)}/tokens/${encodeURIComponent(token)}?confirm=true`, "DELETE");
    renderClients();
  } catch (err) {
    alert(err.message);
  }
}

async function toggleClient(id, wasOn) {
  if (!confirm(wasOn ? `Turn ${id} off? All its tokens stop working until you turn it on.` : `Turn ${id} on again?`)) return;
  try {
    await json(`/api/clients/${encodeURIComponent(id)}`, "PATCH", { enabled: !wasOn, confirm: true });
    renderClients();
  } catch (err) {
    alert(err.message);  // turning one on is for the operator it acts for: the server says so
  }
}

document.getElementById("add-client").addEventListener("click", () => {
  document.getElementById("client-form").reset();
  document.getElementById("client-person").textContent = document.getElementById("whoami-name").textContent || "you";
  openModal("client-modal");
});

document.getElementById("client-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const r = await json("/api/clients", "POST", { kind: f.kind.value, max_clearance: Number(f.max_clearance.value), label: f.label.value.trim(), confirm: true });
    closeModal("client-modal");
    showClientToken(`${r.agent.id}: its first token`, r.settings);
    renderClients();
  } catch (err) {
    alert(err.message);
  }
});

document.getElementById("client-token-copy").addEventListener("click", async (e) => {
  try {
    await navigator.clipboard.writeText(document.getElementById("client-token-settings").textContent);
    e.target.textContent = "Copied";
  } catch (err) {
    e.target.textContent = "Select and copy by hand";
  }
});
document.querySelector('[data-tab="agents"]').addEventListener("click", renderClients);

// ---- policy ----
let policyHash = "";

function policyMatrixHtml(grid, added = [], removed = []) {
  const ids = Object.keys(grid).sort();
  if (!ids.length) return '<p class="empty">No agents with a reach block.</p>';
  const isAdded = (c, t) => added.some(([a, b]) => a === c && b === t);
  const wasRemoved = (c, t) => removed.some(([a, b]) => a === c && b === t);
  const cell = (c, t) => {
    if (c === t) return '<td class="pm-self">·</td>';
    if (grid[c].includes(t)) return `<td class="pm-yes${isAdded(c, t) ? " pm-new" : ""}">${isAdded(c, t) ? "+" : "yes"}</td>`;
    return `<td class="pm-no${wasRemoved(c, t) ? " pm-gone" : ""}">${wasRemoved(c, t) ? "−" : ""}</td>`;
  };
  return `<table class="policy-matrix"><thead><tr><th>caller ↓ / target →</th>${ids.map((i) => `<th>${esc(i)}</th>`).join("")}</tr></thead><tbody>${
    ids.map((c) => `<tr><th>${esc(c)}</th>${ids.map((t) => cell(c, t)).join("")}</tr>`).join("")}</tbody></table>`;
}

function showPolicyResult(r, extra = "") {
  const out = document.getElementById("policy-result");
  const parts = [];
  r.errors.forEach((e) => parts.push(`<div class="finding critical"><span class="sev"></span><span class="summary">${esc(e)}</span></div>`));
  r.problems.forEach((p) => parts.push(`<div class="finding warn"><span class="sev"></span><span class="summary">${esc(p)}</span></div>`));
  if (!r.errors.length) {
    const restarts = r.restarts.length ? `Restarts on next sync: ${r.restarts.map(esc).join(", ")}.` : "No agent restarts.";
    parts.push(`<p>${r.added.length} call path(s) added (+), ${r.removed.length} removed (−). ${restarts}${extra}</p>`);
  }
  out.innerHTML = parts.join("");
  document.getElementById("policy-matrix-wrap").innerHTML = r.matrix ? policyMatrixHtml(r.matrix, r.added, r.removed) : "";
  document.getElementById("policy-health-wrap").innerHTML = r.health ? policyHealthHtml(r.health) : "";
}

// What each agent ends up with from the policy's `health` list.
function policyHealthHtml(health) {
  const ids = Object.keys(health).sort();
  if (!ids.length) return "";
  const span = (s) => s == null ? "never" : s % 86400 === 0 ? `${s / 86400} d` : s % 3600 === 0 ? `${s / 3600} h` : `${Math.round(s / 60)} min`;
  return `<table class="policy-health"><thead><tr><th>agent</th><th>self-healing</th><th>tell people after</th><th>mailbox holds</th><th>card reminder after</th><th>sync self-update</th></tr></thead><tbody>${
    ids.map((id) => `<tr><td>${esc(id)}</td><td>${health[id].heal ? "on" : "off"}</td><td>${esc(span(health[id].quiet_after))}</td><td>${esc(span(health[id].mailbox_ttl))}</td><td>${esc(span(health[id].remind_after))}</td><td>${health[id].update_sync ? "on" : "off"}</td></tr>`).join("")
  }</tbody></table>`;
}

async function renderPolicyTags() {
  const el = document.getElementById("policy-tags");
  const agents = (await api.list("agents")).filter((a) => a.reach);
  const tags = uniqueTags(agents);
  const untagged = agents.filter((a) => !tagsOf(a).length);
  const group = (title, members) => `<div class="tag-group"><span class="chip">${esc(title)}</span> ${members.map((a) => esc(a.id)).join(", ") || "–"}</div>`;
  el.innerHTML = (tags.map((t) => group(t, agents.filter((a) => tagsOf(a).includes(t)))).join("")
    + (untagged.length ? group("no tag", untagged) : "")) || '<p class="empty">No agents in the mesh yet.</p>';
}

async function renderPolicy() {
  renderPolicyTags().catch(() => {});
  try {
    const p = await json("/api/policy");
    document.getElementById("policy-text").value = p.text;
    policyHash = p.hash;
    showPolicyResult({ errors: [], problems: p.problems, matrix: p.matrix, health: p.health, added: [], removed: [], restarts: [] });
  } catch (err) {
    document.getElementById("policy-result").textContent = `Could not load the policy: ${err.message}`;
  }
}

document.getElementById("policy-check").addEventListener("click", async () => {
  try {
    showPolicyResult(await json("/api/policy/preview", "POST", { text: document.getElementById("policy-text").value }));
  } catch (err) {
    alert(err.message);
  }
});

document.getElementById("policy-save").addEventListener("click", async () => {
  const text = document.getElementById("policy-text").value;
  try {
    const first = await json("/api/policy", "PUT", { text, base_hash: policyHash, confirm: false });
    const checked = await json("/api/policy/preview", "POST", { text });
    showPolicyResult({ errors: [], problems: first.problems, matrix: checked.matrix, health: checked.health,
                       added: first.added, removed: first.removed, restarts: first.restarts });
    if (!confirm(`${first.detail}\n\nSave this policy?`)) return;
    const done = await json("/api/policy", "PUT", { text, base_hash: policyHash, confirm: true });
    policyHash = done.hash;
    showPolicyResult(done, " Saved.");
  } catch (err) {
    alert(err.message);
  }
});

document.querySelector('[data-tab="policy"]').addEventListener("click", renderPolicy);

// Notifications (backend/push.py): the browser's own Web Push, so the same page
// added to a phone's Home Screen gets them too. On iPhone and iPad, Safari
// offers push only to a page on the Home Screen; the button says so there.
const notifyButton = document.getElementById("notify");

function b64urlBytes(text) {
  return Uint8Array.from(atob(text.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
}

function sameKey(subscription, key) {
  const had = new Uint8Array(subscription.options.applicationServerKey ?? new ArrayBuffer(0));
  const want = b64urlBytes(key);
  return had.length === want.length && had.every((b, i) => b === want[i]);
}

function showNotify(state) {
  notifyButton.hidden = false;
  notifyButton.disabled = state === "blocked" || state === "home-screen";
  notifyButton.textContent = {
    on: "Notifications on", off: "Notify me", blocked: "Notifications blocked",
    "home-screen": "Add to Home Screen for notifications",
  }[state];
  notifyButton.title = state === "on" ? "Turn notifications off for this browser" : "";
}

async function sendSubscription(subscription) {
  const result = await json("/api/push", "POST", subscription.toJSON());
  if (result.new && !(result.sent >= 200 && result.sent < 300)) {
    alert(`Beacon saved this browser, but its test notification did not go through: ${result.sent}`);
  }
}

async function setupPush() {
  if (!("serviceWorker" in navigator)) return;
  const registration = await navigator.serviceWorker.register("/sw.js");
  if (!("PushManager" in window)) {
    if (navigator.standalone === false) showNotify("home-screen");  // iOS Safari, not on the Home Screen yet
    return;
  }
  const { key } = await json("/api/push");
  if (!key) return;  // this Beacon cannot push
  let subscription = await registration.pushManager.getSubscription();
  if (subscription && !sameKey(subscription, key)) {
    // Beacon's key changed (BEACON_A2A_SECRET was rotated), so this one is dead.
    // Safari subscribes only on a tap, so the person turns it on again.
    await subscription.unsubscribe();
    subscription = null;
  }
  // Sent on every open: it heals a subscription the browser renewed or Beacon lost.
  if (subscription) await sendSubscription(subscription);
  showNotify(subscription ? "on" : Notification.permission === "denied" ? "blocked" : "off");

  notifyButton.addEventListener("click", async () => {
    try {
      const current = await registration.pushManager.getSubscription();
      if (current) {
        await json("/api/push", "DELETE", { endpoint: current.endpoint });
        await current.unsubscribe();
        showNotify("off");
      } else if (await Notification.requestPermission() === "granted") {
        const created = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64urlBytes(key) });
        await sendSubscription(created);
        showNotify("on");
      } else {
        showNotify(Notification.permission === "denied" ? "blocked" : "off");
      }
    } catch (err) {
      alert(err.message);
    }
  });
}

// Who is looking decides what is drawn: a member (backend/members.py) sees their
// own agents and board and nothing else, and the server refuses the rest anyway.
let isMember = false;

async function start() {
  const user = await fetch("/auth/me").then((r) => (r.ok ? r.json() : null)).catch(() => null);
  isMember = user?.role === "member";  // no login (auth off): an operator
  if (user) {
    document.getElementById("whoami-name").textContent = user.name || user.email || user.sub;
    document.getElementById("whoami").hidden = false;
    setupPush().catch((err) => console.warn("notifications:", err));  // optional: the page works without them
  }
  if (isMember) {
    document.body.classList.add("member");
  } else {
    renderHosts();
    renderTemplates();
    renderCronJobs();
    populateHostSelect();
  }
  renderWorkflows();  // the page opens on Workflows
  if (!isMember) renderClients();
  renderDevices();
  renderAgents();
  renderBoard();
}
start();

setInterval(() => {
  updateWorkflowBadge();
  if (document.getElementById("agents-view").classList.contains("active")) renderAgents();
  // Agents change the board too; keep it current while it's open.
  if (document.getElementById("board-view").classList.contains("active")) renderBoard();
}, 30000);

json("/api/version").then((v) => {
  document.getElementById("beacon-version").textContent = `v${v.version}`;
}).catch(() => {}); // cosmetic: no version label beats a broken page
