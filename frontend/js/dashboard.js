async function loadDashboard() {
  await appReady;
  const d = await api("/api/dashboard");
  document.getElementById("subtitle").textContent = `${d.agent || "Your agent"} answers the ${d.company || "TIKIRII"} line, explains the product and books demos.`;
  document.getElementById("key-warning").hidden = d.api_key_set;
  const m = d.messaging || {};
  const problem = [m.sms && `SMS: ${m.sms}`, m.whatsapp && `WhatsApp: ${m.whatsapp}`].filter(Boolean).join(" · ");
  document.getElementById("messaging-warning").hidden = !problem;
  document.getElementById("messaging-warning").textContent = problem ? `Summary messages can't be sent yet. ${problem}` : "";

  document.getElementById("leads-new").textContent = d.leads_new;
  document.getElementById("leads-7d").textContent = d.leads_7d;
  document.getElementById("calls-today").textContent = d.calls_today;
  document.getElementById("calls-7d").textContent = d.calls_7d;

  clear(document.getElementById("new-leads"), d.new_leads.length
    ? d.new_leads.map((x) => el("div", { class: "msg-card" },
        el("div", {}, el("div", { class: "who" }, x.name || "No name", x.business_name ? ` · ${x.business_name}` : ""),
          el("div", { class: "body" }, [x.industry, x.notes].filter(Boolean).join(" · ") || "—"),
          el("div", { class: "meta" }, el("span", {}, x.phone), x.preferred_time ? el("span", {}, `Call back: ${x.preferred_time}`) : null, el("span", {}, fmtDT(x.created_at)))),
        el("div", {}, badge("new"))))
    : el("p", { class: "hint" }, "No new demo requests. When a caller books a demo, it appears here."));

  const calls = document.getElementById("recent-calls");
  clear(calls, d.recent_calls.length
    ? d.recent_calls.map((c) =>
        el("tr", { class: "row-click", onclick: () => (location.href = `call.html?id=${c.id}`) },
          el("td", {}, fmtDT(c.started_at)),
          el("td", {}, CHANNEL_LABELS[c.channel] || c.channel),
          el("td", {}, c.caller_number || "—"),
          el("td", {}, fmtDuration(c.duration_seconds)),
          el("td", {}, badge(c.status))
        ))
    : el("tr", {}, el("td", { colspan: 5, class: "empty" }, "No calls yet.")));

  clear(document.getElementById("setup"),
    el("dt", {}, "Live model"), el("dd", {}, el("code", {}, d.model)),
    el("dt", {}, "Phone (Asterisk)"), el("dd", {}, d.pbx_enabled ? `On — AudioSocket port ${d.pbx_port}` : "Off (set PBX_ENABLED=true in .env)"),
    el("dt", {}, "Demo request alerts"), el("dd", {}, d.lead_alerts ? "On — texted to LEAD_NOTIFY_PHONE" : "Off (set LEAD_NOTIFY_PHONE in .env to get an SMS for each one)"),
    el("dt", {}, "Live calls now"), el("dd", {}, String(d.active_calls))
  );
}

loadDashboard().catch((err) => toast(err.message, true));
setInterval(() => loadDashboard().catch(() => {}), 30000);
