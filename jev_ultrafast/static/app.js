const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let state = null,
  busy = false,
  remoteBusy = false,
  resumeAfterReport = false,
  automatic = false;
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const percent = (value) => `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;
async function call(name, body = {}) {
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify({ ...body, chat_id: state?.chat_id }),
  });
  const data = await response.json();
  if (data.code === "stale_session") {
    automatic = false;
    sessionStorage.setItem("jev-task-draft", JSON.stringify({
      goal: $("goal").value,
    }));
    location.reload();
    throw Error("Refreshing connection. Press Start task when the page reloads.");
  }
  if (!response.ok) {
    if (data.state) { state = data.state; render(); }
    throw Error(data.error || "درخواست ناموفق بود؛ وضعیت اتصال را بررسی کن.");
  }
  state = data;
  render();
  return data;
}
function controls() {
  const live = state?.page && !["done", "blocked"].includes(state.status);
  $("start").disabled = busy || remoteBusy;
  $("new-chat").disabled = busy || remoteBusy;
  $("chat-list").disabled = busy || remoteBusy;
  $("goal").disabled = busy;
  $("choose").disabled = busy || remoteBusy || !live;
  $("execute").disabled = busy || remoteBusy || !state?.decision || !live;
  $("auto").disabled = busy || remoteBusy || !live;
  $("auto").hidden = automatic;
  $("stop").hidden = !automatic;
  $("download").disabled = !state?.history?.length;
}
async function perform(fn, label) {
  if (busy) return;
  busy = true;
  $("error").hidden = true;
  controls();
  $("status").textContent = label;
  try {
    await fn();
  } catch (error) {
    automatic = false;
    try {
      state = await fetch("/api/state").then((r) => r.json());
      render();
    } catch {
      /* Preserve the original failure if the server disconnected. */
    }
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("status").textContent = "Paused · needs attention";
  } finally {
    busy = false;
    controls();
  }
}
function render() {
  if (!state) return;
  $("performance").textContent = Object.entries(state.performance || {}).map(([name, m]) =>
    `${name.includes("jev") ? "Jev" : "LLM"}: ${m.calls} فراخوانی · میانگین ${(m.mean_ms / 1000).toFixed(2)} ثانیه · خطا ${m.failures}`
  ).join(" | ");
  $("chat-list").innerHTML = (state.chats || []).map(c =>
    `<option value="${escape(c.id)}">${escape(c.title)}</option>`).join("");
  $("chat-list").value = state.chat_id;
  $("chat-messages").innerHTML = (state.messages || []).map(m =>
    `<article class="chat-message ${m.role === 'user' ? 'user' : 'assistant'}" dir="auto"><small>${m.role === 'user' ? 'شما' : m.source === 'harness' ? 'گزارش اجرا' : 'DeepSeek'}</small><div>${escape(m.content)}</div></article>`
  ).join("");
  $("helper").textContent = `Text helper · ${state.text_model}`;
  $("plan").innerHTML = (state.plan || [])
    .map(
      (goal, i) =>
        `<div class="plan-step ${i === state.plan_index ? "current" : ""}"><span>${i < state.plan_index ? "✓" : i + 1}</span>${escape(goal)}</div>`,
    )
    .join("");
  const page = state.page,
    d =
      state.decision ||
      (state.status === "done" ? state.decisions?.at(-1) : null);
  const labels = {
    idle: "Ready to explore",
    ready: "Page observed · ready for a decision",
    predicted: "Choice ready · inspect or execute",
    done: "Jev reports complete · inspect the page",
    blocked: "Stopped · no supported next action",
  };
  $("status").textContent = labels[state.status] || state.status;
  if (!page) {
    $("empty").hidden = false;
    $("screenshot").hidden = true;
    $("screenshot").removeAttribute("src");
    $("targets").innerHTML = "";
    $("choices").innerHTML = "";
    $("operation-choices").innerHTML = "";
    $("choice-title").textContent = "منتظر درخواست";
    $("url").textContent = "مرورگر این گفتگو";
    $("page-title").textContent = "هنوز صفحه‌ای باز نشده";
    controls();
    return;
  }
  $("empty").hidden = true;
  $("screenshot").hidden = false;
  $("screenshot").src = `data:image/jpeg;base64,${page.screenshot}`;
  $("url").textContent = page.url;
  $("page-title").textContent = page.title;
  $("action-count").textContent = `${state.elements.length} elements`;
  const chosen = page.actions.find((a) => a.id === d?.choice);
  $("choice-title").textContent = d
    ? chosen?.label || d.choice
    : "Choose an action";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("ranking-note").textContent = d ? "Ranked by Jev" : "Unranked";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a,b)=>b[1]-a[1]);
  $("operation-choices").innerHTML = op.map(([name,p]) =>
    `<span class="operation-choice ${name === d.operation ? 'best' : ''}">${escape(name)} <b>${percent(p)}</b></span>`).join('');
  const probability = e => d?.target_probabilities[e.index] ??
    Math.max(-1, ...(e.options || []).map(o=>d?.target_probabilities[o.index] ?? -1));
  const selectedIndex = d?.target?.split(':')[0];
  const elements = [...state.elements];
  if (d) elements.sort((a,b)=>probability(b)-probability(a));
  $("choices").innerHTML = elements.map(e => {
    const p = probability(e);
    return `<div class="choice ${selectedIndex === e.index ? 'best' : ''}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(e.role)} · ${escape(e.operations.join(' / '))}${e.value ? ' · '+escape(e.value) : ''}${e.checked !== undefined ? ' · checked '+escape(e.checked) : ''}</small>${p >= 0 ? `<div class="bar" style="--probability:${p*100}%"></div>` : ''}</div><span class="probability">${p >= 0 ? percent(p) : '—'}</span></div>`;
  }).join('');
  const targets = new Map();
  for (const a of page.actions) if (a.rect && !targets.has(a.node)) targets.set(a.node, a);
  $("targets").innerHTML = [...targets.values()].map((a,i) => {
    const index=String(i+1);
    return `<div class="target ${index === selectedIndex ? 'selected' : ''}" data-action="${index}" style="left:${100*a.rect.x/page.w}%;top:${100*a.rect.y/page.h}%;width:${100*a.rect.w/page.w}%;height:${100*a.rect.h/page.h}%"><span>${index}</span></div>`;
  }).join('');
  $("targets").hidden = !$("overlays").checked;
  $("history").innerHTML = state.history.length
    ? state.history
        .map(
          (h) =>
            `<div class="trace-row"><span class="number">${String(h.step).padStart(2, "0")}</span><div>${escape(h.action)}${h.text ? ` <b>“${escape(h.text)}”</b><small>${escape(h.text_helper)}</small>` : ""}</div><span class="time">${h.latency_ms} ms · ${percent(h.probability)}</span><span class="effect">${h.page_changed ? "Page changed" : "No change observed"}</span></div>`,
        )
        .join("")
    : '<p class="muted">Each executed action leaves an observed result.</p>';
  $("step-count").textContent = `${state.history.length} actions · ${(state.elapsed_ms / 1000).toFixed(2)} s`;
  $("model-state").textContent = JSON.stringify(
    d?.request || {
      goal: state.goal,
      url: page.url,
      text: page.text,
      actions: page.actions.map(({ rect, node, ...rest }) => rest),
    },
    null,
    2,
  );
  controls();
}
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  automatic = false;
  resumeAfterReport = true;
  perform(
    async () => {
      await call("chat", { message: $("goal").value });
      $("goal").value = "";
    },
    "در حال فهم درخواست و آماده‌کردن جست‌وجو…",
  ).then(() => {
    if (!$("error").hidden || !state?.start_run) return;
    $("auto").click();
  });
});
$("choose").addEventListener("click", () =>
  perform(() => call("predict"), "Jev is comparing the actions…"),
);
$("execute").addEventListener("click", () =>
  perform(
    () => call("act", { fingerprint: state.page.fingerprint }),
    "Executing the choice…",
  ),
);
$("auto").addEventListener("click", () =>
  perform(async () => {
    automatic = true;
    resumeAfterReport = true;
    controls();
    for (let i = 0; i < state.max_steps * 2 && automatic; i++) {
      $("status").textContent = "Running…";
      if ($("pace").checked) {
        await call("predict");
        await new Promise(resolve => setTimeout(resolve, 450));
        if (!automatic) break;
        await call("act", {fingerprint: state.page.fingerprint});
      } else {
        await call("tick");
      }
      if (["done", "blocked"].includes(state.status)) break;
    }
    automatic = false;
    await call("summary");
  }, "Running the browser…"),
);
$("stop").addEventListener("click", () => {
  automatic = false;
  resumeAfterReport = false;
  $("status").textContent = "Pausing after the current request…";
  controls();
});
$("overlays").addEventListener("change", () => {
  $("targets").hidden = !$("overlays").checked;
});
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === id || t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    );
});
$("choices").addEventListener("pointerleave", () =>
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    ),
);
$("download").addEventListener("click", () => {
  const { page, ...rest } = state;
  const blob = new Blob(
    [
      JSON.stringify(
        { ...rest, page: { ...page, screenshot: undefined } },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "typesafe-browser-trace.json";
  a.click();
  URL.revokeObjectURL(url);
});
fetch("/api/state")
  .then((r) => r.json())
  .then((s) => {
    state = s;
    render();
  })
  .catch(() => {
    $("status").textContent = "Cannot reach local demo server";
  });

try {
  const draft = JSON.parse(sessionStorage.getItem("jev-task-draft") || "null");
  if (draft) {
    $("goal").value = draft.goal;
    sessionStorage.removeItem("jev-task-draft");
    $("error").textContent = "Connection refreshed. Press Start task to run your saved request.";
    $("error").hidden = false;
  }
} catch { /* Ignore unavailable storage or invalid drafts. */ }

let checkingReply = false;
setInterval(async () => {
  const waitingForRecovery = resumeAfterReport && state?.status === "ready";
  if (busy || checkingReply || (!state?.messages?.some(m => m.pending) && !waitingForRecovery)) return;
  checkingReply = true;
  try {
    const fresh = await fetch("/api/state").then(r => r.json());
    if (!busy) {
      state = fresh;
      remoteBusy = Boolean(fresh.busy);
      render();
      if (resumeAfterReport && !remoteBusy && state.status === "ready" && !state.messages.some(m => m.pending)) {
        $("auto").click();
      }
    }
  } catch { /* Keep the current page; retry this read on the next poll. */ }
  finally { checkingReply = false; }
}, 750);

let checkingActivity = false;
setInterval(async () => {
  if (busy || checkingActivity) return;
  checkingActivity = true;
  try {
    const activity = await fetch("/api/activity").then(r => r.json());
    remoteBusy = activity.busy;
    controls();
  } catch { /* Keep the last known state. */ }
  finally { checkingActivity = false; }
}, 750);

async function selectChat(name, body = {}) {
  automatic = false;
  resumeAfterReport = false;
  await perform(async () => {
    await call(name, body);
    $("goal").value = "";
    $("goal").focus();
  }, "در حال بازکردن گفتگو…");
}
$("new-chat").addEventListener("click", () => selectChat("new_chat"));
$("chat-list").addEventListener("change", () => selectChat("switch_chat", {id: $("chat-list").value}));
