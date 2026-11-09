// The company & agent page: one form for the profile, plus the recordings, which upload on their own.

const LANGS = ["si", "ta", "en"];
const form = document.getElementById("profile-form");
let profile = null, meta = null;

function perLanguage(containerId, namePrefix, values, placeholders, rows = 2, maxlength = 500) {
  clear(document.getElementById(containerId), LANGS.map((code) =>
    el("div", { style: "margin-bottom:8px" },
      el("span", { class: "hint" }, meta.language_names[code]),
      el("textarea", { name: `${namePrefix}_${code}`, rows, maxlength, placeholder: placeholders[code] || "" }, values?.[code] || ""))));
}

function audioRow(url, hasFile, onUpload, onDelete, label) {
  const input = el("input", { type: "file", accept: "audio/*", hidden: true, onchange: async () => { if (input.files[0]) await onUpload(input.files[0]); input.value = ""; } });
  return el("div", { class: "audio-row" },
    label ? el("strong", {}, label) : null,
    hasFile ? el("audio", { controls: true, preload: "none", src: `${url}?t=${Date.now()}` }) : el("span", { class: "hint" }, "No recording"),
    el("button", { type: "button", class: "small", onclick: () => input.click() }, hasFile ? "Replace" : "Upload"), input,
    hasFile ? el("button", { type: "button", class: "small danger", onclick: onDelete }, "Delete") : null);
}

async function upload(path, file) {
  const body = new FormData();
  body.append("file", file);
  try { profile = await api(path, { method: "POST", body }); toast("Uploaded."); renderAudio(); } catch (e) { toast(e.message, true); }
}
async function remove(path) {
  try { profile = await api(path, { method: "DELETE" }); toast("Deleted."); renderAudio(); } catch (e) { toast(e.message, true); }
}

function renderAudio() {
  clear(document.getElementById("menu-audio"), audioRow("/api/profile/menu-audio", !!profile.menu_audio,
    (f) => upload("/api/profile/menu-audio", f), () => remove("/api/profile/menu-audio")),
    el("p", { class: "hint", style: "margin:0" }, "MP3, M4A or WAV, up to 60 seconds. If calls are recorded, say so in this clip: the agent never says it."));
  clear(document.getElementById("unavailable"), LANGS.map((code) => audioRow(`/api/profile/unavailable-audio/${code}`, !!profile.unavailable_audio?.[code],
    (f) => upload(`/api/profile/unavailable-audio/${code}`, f), () => remove(`/api/profile/unavailable-audio/${code}`), meta.language_names[code])));
}

function fill() {
  fillForm(form, profile);
  perLanguage("greetings", "greeting", profile.greetings, meta.default_greetings, 6, 2000);
  clear(document.getElementById("agent_voice"), el("option", { value: "" }, `Default (${meta.default})`),
    meta.voices.map((v) => el("option", { value: v.name }, `${v.name} · ${v.gender} · ${v.style}`)));
  form.elements.agent_voice.value = profile.agent_voice;
  clear(document.getElementById("languages"), LANGS.map((code) =>
    el("label", {}, el("input", { type: "checkbox", id: `lang_${code}`, checked: profile.languages_enabled.includes(code), onchange: syncMain }), ` ${meta.language_names[code]}`)));
  clear(document.getElementById("keys"), LANGS.map((code) =>
    el("div", {}, el("span", { class: "hint" }, meta.language_names[code]),
      el("input", { type: "text", name: `key_${code}`, maxlength: 1, value: profile.menu_keys?.[code] || "", style: "max-width:80px" }))));
  clear(document.getElementById("preview-lang"), LANGS.map((code) => el("option", { value: code }, meta.language_names[code])));
  syncMain();
  renderAudio();
}

function enabled() { return LANGS.filter((c) => document.getElementById(`lang_${c}`)?.checked); }

function syncMain() {
  const select = document.getElementById("default_language");
  const current = select.value || profile.default_language;
  clear(select, enabled().map((c) => el("option", { value: c }, meta.language_names[c])));
  select.value = enabled().includes(current) ? current : (enabled()[0] || "");
}

function collect() {
  const body = formData(form);
  const out = {};
  for (const [k, v] of Object.entries(body)) if (!/^(greeting|notice|key)_/.test(k)) out[k] = v;
  out.greetings = Object.fromEntries(LANGS.map((c) => [c, body[`greeting_${c}`] || ""]));
  out.recording_notices = Object.fromEntries(LANGS.map((c) => [c, body[`notice_${c}`] || ""]));
  out.menu_keys = Object.fromEntries(LANGS.map((c) => [c, body[`key_${c}`] || ""]).filter(([, k]) => k));
  out.languages_enabled = enabled();
  return out;
}

async function save(e) {
  e?.preventDefault();
  try {
    profile = await api("/api/profile", { method: "PUT", body: collect() });
    toast("Saved. The next call will use it.");
    fill();
  } catch (err) { toast(err.message, true); }
}

async function preview() {
  const lang = document.getElementById("preview-lang").value;
  try {
    const p = await api(`/api/profile/prompt-preview?language=${lang}`);
    document.getElementById("preview-info").textContent = `${p.chars.toLocaleString()} characters (about ${p.approx_tokens.toLocaleString()} tokens) · model ${p.model} · voice ${p.voice} · tools: ${p.tools.join(", ")}${p.warning ? ` · ${p.warning}` : ""}`;
    const box = document.getElementById("preview");
    box.textContent = `${p.system_instruction}\n\n---- first thing sent when the caller connects ----\n${p.greeting_note}`;
    box.hidden = false;
  } catch (err) { toast(err.message, true); }
}

(async () => {
  await appReady;
  [profile, meta] = await Promise.all([api("/api/profile"), api("/api/voices")]);
  fill();
  api("/api/messaging").then((m) => {
    const problem = [m.sms && `SMS: ${m.sms}`, m.whatsapp && `WhatsApp: ${m.whatsapp}`].filter(Boolean).join(" · ");
    const box = document.getElementById("messaging-problem");
    box.hidden = !problem;
    box.textContent = problem ? `Not ready yet: ${problem}. Callers are told the message cannot be sent and hear the details instead.` : "";
  }).catch(() => {});
})().catch((e) => toast(e.message, true));

form.addEventListener("submit", save);
document.getElementById("save-top").addEventListener("click", () => save());
document.getElementById("preview-btn").addEventListener("click", preview);
