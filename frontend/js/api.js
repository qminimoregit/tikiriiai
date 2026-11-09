// Shared helpers: API calls, sidebar, toasts, DOM builder. Data from the server is always inserted as text
// (el() / textContent), never as HTML: names, messages and transcripts come from callers and admins.

const NAV = [
  ["Overview", [["index.html", "Dashboard"], ["leads.html", "Demo requests"], ["calls.html", "Calls"], ["messages.html", "Messages sent"]]],
  ["The agent", [["profile.html", "Company & agent"]]],
  ["What the agent tells callers", [["capabilities.html", "What it does"], ["industries.html", "Industries"], ["integrations.html", "Integrations"], ["faq.html", "FAQ"]]],
  ["Try it", [["test-call.html", "Test call"]]],
];

const LANG_LABELS = { en: "English", si: "සිංහල", ta: "தமிழ்" };
const CHANNEL_LABELS = { browser_admin: "Test call", pbx: "Phone" };
const CHANNEL_NAMES = { sms: "SMS", whatsapp: "WhatsApp" };

let APP_TZ = "Asia/Colombo";
const appReady = fetch("/api/auth/me")
  .then((r) => r.json())
  .then((me) => {
    if (me.timezone) APP_TZ = me.timezone;
    return me;
  })
  .catch(() => ({}));

async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    location.href = `/login.html?next=${encodeURIComponent(location.pathname + location.search)}`;
    throw new Error("Please sign in");
  }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = typeof data === "object" ? formatError(data.detail) : data;
    throw new Error(msg || res.statusText);
  }
  return data;
}

function formatError(detail) {
  if (Array.isArray(detail)) return detail.map((d) => (d.loc?.length > 1 ? `${d.loc.slice(-1)[0]}: ` : "") + d.msg.replace(/^Value error, /, "")).join("; ");
  return detail;
}

// el("div", {class: "x", onclick: fn}, "text", childNode, [more children])
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

function renderSidebar() {
  const here = location.pathname.split("/").pop() || "index.html";
  const nav = el("nav", { class: "sidebar" },
    el("div", { class: "brand" }, el("span", { class: "brand-mark" }, "T"), "TIKIRII"),
    NAV.map(([group, links]) => [
      el("div", { class: "nav-label" }, group),
      links.map(([href, label]) => {
        return el("a", { href, class: href === here ? "active" : "" }, label);
      }),
    ]),
    el("a", {
      href: "#", class: "signout",
      onclick: async (e) => {
        e.preventDefault();
        await fetch("/api/auth/logout", { method: "POST" });
        location.href = "/login.html";
      },
    }, "Sign out")
  );
  document.body.prepend(nav);
}

function toast(message, isError = false) {
  let node = document.querySelector(".toast");
  if (!node) {
    node = el("div", { class: "toast" });
    document.body.appendChild(node);
  }
  node.textContent = message;
  node.classList.toggle("err", isError);
  node.classList.add("show");
  clearTimeout(node._t);
  node._t = setTimeout(() => node.classList.remove("show"), 3200);
}

function fmtDT(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-GB", { timeZone: APP_TZ, day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit", hour12: true });
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

function badge(text, cls = text) {
  return el("span", { class: `badge ${cls}` }, String(text).replaceAll("_", " "));
}

// Read all named inputs inside a form into an object.
function formData(form) {
  const out = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.type === "checkbox") {
      if (input.dataset.group) {
        out[input.dataset.group] ??= [];
        if (input.checked) out[input.dataset.group].push(input.value);
      } else out[input.name] = input.checked;
    } else if (input.type === "radio") {
      if (input.checked) out[input.name] = input.value;
    } else if (input.type === "number") out[input.name] = input.value === "" ? null : Number(input.value);
    else out[input.name] = input.value;
  }
  return out;
}

// Fill named inputs from an object. Grouped checkboxes (data-group) are ticked from an array.
function fillForm(form, data) {
  for (const input of form.elements) {
    if (input.type === "checkbox" && input.dataset.group) {
      if (input.dataset.group in data) input.checked = (data[input.dataset.group] || []).map(String).includes(input.value);
      continue;
    }
    if (!input.name || !(input.name in data)) continue;
    const value = data[input.name];
    if (input.type === "checkbox") input.checked = !!value;
    else if (input.type === "radio") input.checked = input.value === String(value);
    else input.value = value ?? "";
  }
}

async function confirmAction(message) {
  return window.confirm(message);
}

renderSidebar();
