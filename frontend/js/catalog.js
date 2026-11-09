// Shared list page for capabilities, industries, integrations and FAQ entries: list, add/edit dialog, show/hide, reorder, delete.

// toForm(item): the item as the form shows it (e.g. a list as one line per entry); default: as it is.
function catalogPage({ endpoint, singular, empty, defaults, render, before, afterLoad, toForm = (x) => x }) {
  const listEl = document.getElementById("list");
  const dialog = document.getElementById("item-dialog");
  const form = document.getElementById("item-form");
  let items = [];
  let editingId = null;

  async function load() {
    items = await api(endpoint);
    clear(listEl, items.length
      ? items.map((item, i) =>
          el("div", { class: `item-row${item.is_active ? "" : " inactive"}` },
            el("div", { class: "order-btns" },
              el("button", { type: "button", title: "Move up", disabled: i === 0, onclick: () => move(i, -1) }, "▲"),
              el("button", { type: "button", title: "Move down", disabled: i === items.length - 1, onclick: () => move(i, 1) }, "▼")),
            el("div", { class: "item-main" }, render(item), item.is_active ? null : el("div", { class: "meta" }, badge("hidden from callers", "inactive"))),
            el("div", { class: "actions" },
              el("button", { type: "button", class: "small", onclick: () => toggle(item) }, item.is_active ? "Hide" : "Show"),
              el("button", { type: "button", class: "small", onclick: () => openDialog(item) }, "Edit"),
              el("button", { type: "button", class: "small danger", onclick: () => remove(item) }, "Delete"))
          ))
      : el("p", { class: "hint", style: "margin:0" }, empty));
    afterLoad?.(items);
  }

  function payload(item) {
    const { id, sort_order, ...rest } = item;
    return rest;
  }

  async function move(i, delta) {
    const ids = items.map((x) => x.id);
    [ids[i], ids[i + delta]] = [ids[i + delta], ids[i]];
    try {
      await api(`${endpoint}/reorder`, { method: "POST", body: { ids } });
      await load();
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function toggle(item) {
    try {
      await api(`${endpoint}/${item.id}`, { method: "PUT", body: { ...payload(item), is_active: !item.is_active } });
      await load();
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function remove(item) {
    if (!(await confirmAction(`Delete this ${singular}? This can't be undone.`))) return;
    try {
      await api(`${endpoint}/${item.id}`, { method: "DELETE" });
      await load();
    } catch (err) {
      toast(err.message, true);
    }
  }

  function openDialog(item = null) {
    editingId = item ? item.id : null;
    document.getElementById("dialog-title").textContent = item ? `Edit ${singular}` : `Add ${singular}`;
    form.reset();
    fillForm(form, toForm(item || defaults));
    dialog.showModal();
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = formData(form);
    try {
      if (editingId) await api(`${endpoint}/${editingId}`, { method: "PUT", body });
      else await api(endpoint, { method: "POST", body });
      dialog.close();
      toast("Saved. The next call will use it.");
      await load();
    } catch (err) {
      toast(err.message, true);
    }
  });

  document.getElementById("add-btn").addEventListener("click", () => openDialog());
  document.getElementById("cancel-btn").addEventListener("click", () => dialog.close());
  Promise.resolve(before?.()).then(load).catch((err) => toast(err.message, true));
}
