const api = {
  async list(kind) {
    const r = await fetch(`/api/${kind}`);
    return r.json();
  },
  async put(kind, id, body) {
    const r = await fetch(`/api/${kind}/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
  async del(kind, id) {
    const r = await fetch(`/api/${kind}/${id}`, { method: "DELETE" });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  },
  async agentStatus(id) {
    const r = await fetch(`/api/agents/${id}/status`);
    if (!r.ok) return { state: "unreachable", detail: (await r.json()).detail };
    return r.json();
  },
  async agentLogs(id, lines = 200) {
    const r = await fetch(`/api/agents/${id}/logs?lines=${lines}`);
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return (await r.json()).text;
  },
  async reconcile(id) {
    const r = await fetch(`/api/agents/${id}/reconcile`);
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
  async applyFix(id, fix) {
    const r = await fetch(`/api/agents/${id}/reconcile`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fix, confirm: true }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
  async getAgent(id) {
    const r = await fetch(`/api/agents/${id}`);
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
  async configDiff(id) {
    const r = await fetch(`/api/agents/${id}/config-diff`);
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
  async restart(id) {
    const r = await fetch(`/api/agents/${id}/restart?confirm=true`, { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
  async listPlugins(id) {
    const r = await fetch(`/api/agents/${id}/plugins`);
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
  async updatePlugin(id, plugin) {
    const r = await fetch(`/api/agents/${id}/plugins/${plugin}/update?confirm=true`, { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
};

function statusPill(state) {
  const label = { "not-installed": "not installed", crashlooping: "crash-looping" }[state] || state;
  return `<span class="status-pill ${state}"><span class="dot"></span>${label}</span>`;
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
  const hosts = await api.list("hosts");
  const body = document.getElementById("hosts-body");
  body.innerHTML = "";
  document.getElementById("hosts-empty").hidden = hosts.length > 0;
  for (const h of hosts) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${h.id}</td>
      <td>${h.address}</td>
      <td>${h.ssh.user}</td>
      <td>${h.ssh.key}</td>
      <td>${h.ssh.port}</td>
      <td>${h.tags.join(", ")}</td>
      <td class="row-actions">
        <button class="link-btn" data-edit="${h.id}">Edit</button>
        <button class="link-btn danger" data-del="${h.id}">Delete</button>
      </td>`;
    body.appendChild(tr);
  }
  body.querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => editHost(b.dataset.edit))
  );
  body.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", () => deleteHost(b.dataset.del))
  );
  return hosts;
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
  const hosts = await api.list("hosts");
  fillHostForm(hosts.find((h) => h.id === id));
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
    tags: f.tags.value.split(",").map((t) => t.trim()).filter(Boolean),
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
  select.innerHTML = hosts.map((h) => `<option value="${h.id}">${h.id}</option>`).join("");
  if (hosts.some((h) => h.id === current)) select.value = current;
}

async function renderAgents() {
  const agents = await api.list("agents");
  const body = document.getElementById("agents-body");
  body.innerHTML = "";
  document.getElementById("agents-empty").hidden = agents.length > 0;
  for (const a of agents) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td id="status-${a.id}">${statusPill("loading")}</td>
      <td>${a.id}</td>
      <td>${a.type}</td>
      <td>${a.host}</td>
      <td>${a.profile ?? ""}</td>
      <td>${a.owner ?? ""}</td>
      <td class="row-actions">
        <button class="link-btn" data-inspect="${a.id}">Inspect</button>
        <button class="link-btn" data-edit="${a.id}">Edit</button>
        <button class="link-btn danger" data-del="${a.id}">Delete</button>
      </td>`;
    body.appendChild(tr);
  }
  body.querySelectorAll("[data-inspect]").forEach((b) =>
    b.addEventListener("click", () => openInspect(b.dataset.inspect))
  );
  body.querySelectorAll("[data-edit]").forEach((b) =>
    b.addEventListener("click", () => editAgent(b.dataset.edit))
  );
  body.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", () => deleteAgent(b.dataset.del))
  );
  refreshStatuses(agents);
  return agents;
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
  const deployOut = document.getElementById("inspect-deploy-output");
  deployOut.hidden = true;
  deployOut.textContent = "";
  const restartOut = document.getElementById("inspect-restart-output");
  restartOut.hidden = true;
  restartOut.textContent = "";
  document.getElementById("inspect-plugins-results").innerHTML = "";
  const updateAgentOut = document.getElementById("inspect-update-agent-output");
  updateAgentOut.hidden = true;
  updateAgentOut.textContent = "";
  document.getElementById("inspect-reconcile-results").innerHTML = "";
  document.getElementById("inspect-config-results").innerHTML = "";
  const configPushOut = document.getElementById("inspect-config-push-output");
  configPushOut.hidden = true;
  configPushOut.textContent = "";
  const decommissionOut = document.getElementById("inspect-decommission-output");
  decommissionOut.hidden = true;
  decommissionOut.textContent = "";
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
  el.innerHTML = "";
  if (!result.reachable) {
    el.innerHTML = `<div class="finding critical"><span class="sev"></span><span class="summary">${result.detail}</span></div>`;
    return;
  }
  for (const c of result.config) {
    const detail = c.status === "match" ? "" : ` — live: ${JSON.stringify(c.live)}, desired: ${JSON.stringify(c.desired)}`;
    const row = document.createElement("div");
    row.className = `finding ${CONFIG_SEVERITY[c.status] || "info"}`;
    row.innerHTML = `<span class="sev"></span><span class="summary"><code>${c.path}</code> ${c.status}${detail}</span>`;
    el.appendChild(row);
  }
  for (const e of result.env) {
    const row = document.createElement("div");
    row.className = `finding ${CONFIG_SEVERITY[e.status] || "info"}`;
    row.innerHTML = `<span class="sev"></span><span class="summary"><code>${e.key}</code> ${e.status} in .env</span>`;
    el.appendChild(row);
  }
  if (!result.config.length && !result.env.length) {
    el.innerHTML = '<div class="finding info"><span class="sev"></span><span class="summary">nothing declared in desired.config / env_keys</span></div>';
  }
}

async function runConfigCheck() {
  if (!currentInspectId) return;
  const el = document.getElementById("inspect-config-results");
  el.innerHTML = '<div class="finding info"><span class="sev"></span><span class="summary">checking…</span></div>';
  try {
    renderConfigFindings(await api.configDiff(currentInspectId));
  } catch (err) {
    el.innerHTML = `<div class="finding critical"><span class="sev"></span><span class="summary">${err.message}</span></div>`;
  }
}

document.getElementById("inspect-config-diff").addEventListener("click", runConfigCheck);

document.getElementById("inspect-config-push").addEventListener("click", async (e) => {
  if (!currentInspectId) return;
  if (!confirm(`Push desired config to ${currentInspectId}? Runs "hermes config set" for every declared key, then restarts the gateway if it's active.`)) return;
  const btn = e.target;
  const out = document.getElementById("inspect-config-push-output");
  out.hidden = false;
  out.textContent = "";
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/agents/${currentInspectId}/config-diff?confirm=true`, { method: "POST" });
    if (!resp.ok) {
      out.textContent = `[error] ${(await resp.json()).detail || resp.statusText}`;
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      out.textContent += decoder.decode(value, { stream: true });
      out.scrollTop = out.scrollHeight;
    }
  } catch (err) {
    out.textContent += `\n[error] ${err.message}`;
  } finally {
    btn.disabled = false;
    runConfigCheck();
  }
});

function renderFindings(findings) {
  const el = document.getElementById("inspect-reconcile-results");
  el.innerHTML = "";
  for (const f of findings) {
    const row = document.createElement("div");
    row.className = `finding ${f.severity}`;
    row.innerHTML = `
      <span class="sev"></span>
      <span class="summary">${f.summary}</span>
      ${f.fix ? `<button type="button" class="link-btn" data-fix="${f.fix}">Fix: ${f.fix}</button>` : ""}
    `;
    el.appendChild(row);
  }
  el.querySelectorAll("[data-fix]").forEach((btn) =>
    btn.addEventListener("click", () => runFix(btn.dataset.fix, btn))
  );
}

async function runCheck() {
  if (!currentInspectId) return;
  const el = document.getElementById("inspect-reconcile-results");
  el.innerHTML = '<div class="finding info"><span class="sev"></span><span class="summary">checking…</span></div>';
  try {
    renderFindings(await api.reconcile(currentInspectId));
  } catch (err) {
    el.innerHTML = `<div class="finding critical"><span class="sev"></span><span class="summary">${err.message}</span></div>`;
  }
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
  const btn = e.target;
  const out = document.getElementById("inspect-deploy-output");
  out.hidden = false;
  out.textContent = "";
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/agents/${currentInspectId}/deploy?confirm=true`, { method: "POST" });
    if (!resp.ok) {
      out.textContent = `[error] ${(await resp.json()).detail || resp.statusText}`;
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      out.textContent += decoder.decode(value, { stream: true });
      out.scrollTop = out.scrollHeight;
    }
  } catch (err) {
    out.textContent += `\n[error] ${err.message}`;
  } finally {
    btn.disabled = false;
    refreshStatuses(await api.list("agents"));
  }
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

function renderPlugins(plugins) {
  const el = document.getElementById("inspect-plugins-results");
  el.innerHTML = "";
  if (!plugins.length) {
    el.innerHTML = '<div class="finding info"><span class="sev"></span><span class="summary">no plugins</span></div>';
    return;
  }
  const sevFor = { enabled: "ok", disabled: "warn" };
  for (const p of plugins) {
    const row = document.createElement("div");
    row.className = `finding ${sevFor[p.status] || "info"}`;
    const canUpdate = p.source === "git";
    row.innerHTML = `
      <span class="sev"></span>
      <span class="summary"><code>${p.name}</code> v${p.version} — ${p.status} (${p.source})</span>
      ${canUpdate ? `<button type="button" class="link-btn" data-update-plugin="${p.name}">Update</button>` : ""}
    `;
    el.appendChild(row);
  }
  el.querySelectorAll("[data-update-plugin]").forEach((btn) =>
    btn.addEventListener("click", () => runUpdatePlugin(btn.dataset.updatePlugin, btn))
  );
}

async function runListPlugins() {
  if (!currentInspectId) return;
  const el = document.getElementById("inspect-plugins-results");
  el.innerHTML = '<div class="finding info"><span class="sev"></span><span class="summary">loading…</span></div>';
  try {
    renderPlugins(await api.listPlugins(currentInspectId));
  } catch (err) {
    el.innerHTML = `<div class="finding critical"><span class="sev"></span><span class="summary">${err.message}</span></div>`;
  }
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
  const btn = e.target;
  const out = document.getElementById("inspect-update-agent-output");
  out.hidden = false;
  out.textContent = "";
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/agents/${currentInspectId}/update?confirm=true`, { method: "POST" });
    if (!resp.ok) {
      out.textContent = `[error] ${(await resp.json()).detail || resp.statusText}`;
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      out.textContent += decoder.decode(value, { stream: true });
      out.scrollTop = out.scrollHeight;
    }
  } catch (err) {
    out.textContent += `\n[error] ${err.message}`;
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("inspect-decommission").addEventListener("click", async (e) => {
  if (!currentInspectId) return;
  const id = currentInspectId;
  if (!confirm(`Decommission ${id}? This stops and uninstalls its gateway service on the host, then archives its Beacon record.`)) return;

  const agent = await api.getAgent(id);
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

  const btn = e.target;
  const out = document.getElementById("inspect-decommission-output");
  out.hidden = false;
  out.textContent = "";
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/agents/${id}/decommission`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ purge, remove_user: removeUser, confirm: true }),
    });
    if (!resp.ok) {
      out.textContent = `[error] ${(await resp.json()).detail || resp.statusText}`;
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      out.textContent += decoder.decode(value, { stream: true });
      out.scrollTop = out.scrollHeight;
    }
  } catch (err) {
    out.textContent += `\n[error] ${err.message}`;
  } finally {
    btn.disabled = false;
    closeModal("inspect-modal");
    renderAgents();
  }
});

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
    desired,
  };
  try {
    await api.put("agents", body.id, body);
    closeModal("agent-modal");
    renderAgents();
  } catch (err) {
    alert(err.message);
  }
});

renderHosts();
renderAgents();
populateHostSelect();

fetch("/auth/me").then((r) => (r.ok ? r.json() : null)).then((user) => {
  if (!user) return; // auth disabled — nothing to show
  document.getElementById("whoami-name").textContent = user.name || user.email || user.sub;
  document.getElementById("whoami").hidden = false;
});

setInterval(() => {
  if (document.getElementById("agents-view").classList.contains("active")) renderAgents();
}, 30000);
