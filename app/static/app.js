// Pestañas de sector y ordenación: actualizan campos ocultos del formulario y lo relanzan.
function setFilter(name, value) {
  const form = document.getElementById("filters");
  form.elements[name].value = value;
  form.dispatchEvent(new Event("change", { bubbles: true }));
}
function sortBy(column) {
  const form = document.getElementById("filters");
  const desc = form.elements.sort.value === column ? (form.elements.desc.value === "1" ? "0" : "1") : "1";
  form.elements.desc.value = desc;
  setFilter("sort", column);
}

// Filtros plegables: abiertos por defecto en escritorio; el contador indica cuántos hay activos.
function updateFilterCount() {
  const box = document.getElementById("filters-box");
  const badge = document.getElementById("filters-count");
  if (!box || !badge) return;
  const active = new Set();
  for (const el of box.querySelectorAll("input, select")) {
    if ((el.type === "checkbox" && el.checked) || (el.type !== "checkbox" && el.value)) active.add(el.closest("label, fieldset") || el);
  }
  badge.textContent = active.size;
  badge.hidden = active.size === 0;
}
document.addEventListener("DOMContentLoaded", () => {
  const box = document.getElementById("filters-box");
  if (!box) return;
  if (window.matchMedia("(min-width: 768px)").matches) box.open = true;
  updateFilterCount();
  box.addEventListener("input", updateFilterCount);
  box.addEventListener("change", updateFilterCount);
});

// --- Aviso emergente (toast) y registro ------------------------------------------------
let toastTimer;
function showToast(message, linkHref, linkText) {
  const toast = document.getElementById("toast");
  toast.replaceChildren();
  const text = document.createElement("span");
  text.textContent = message;
  toast.append(text);
  if (linkHref) {
    const link = document.createElement("a");
    link.href = linkHref;
    link.textContent = linkText;
    toast.append(" ", link);
  }
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Cerrar aviso");
  close.textContent = "×";
  close.onclick = () => (toast.hidden = true);
  toast.append(close);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (toast.hidden = true), 10000);
}
function needLogin() {
  const root = document.body.dataset.root || "";
  const here = location.pathname.slice(root.length) + location.search;
  showToast(
    "Necesitas estar registrado para añadir empresas a seguimiento.",
    `${root}/login?next=${encodeURIComponent(here)}&reason=watch`,
    "Entrar o crear cuenta"
  );
}

// --- Filtros guardados (solo usuarios con sesión) ---------------------------------------
function applySavedFilter(select) {
  location.href = select.value;
}
async function postFilter(url, body) {
  try {
    const res = await fetch(url, { method: "POST", body, credentials: "same-origin" });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.url) {
      location.href = data.url;
    } else if (res.status === 401) {
      needLogin();
    } else {
      showToast(data.error || "No se ha podido completar la acción.");
    }
  } catch {
    showToast("Sin conexión: inténtalo de nuevo.");
  }
}
function saveFilter() {
  const root = document.body.dataset.root || "";
  const name = document.getElementById("saved-name").value.trim();
  if (!name) {
    showToast("Escribe un nombre para el filtro.");
    document.getElementById("saved-name").focus();
    return;
  }
  const data = new URLSearchParams(new FormData(document.getElementById("filters")));
  data.set("name", name);
  postFilter(`${root}/screener/filters`, data);
}
function deleteSavedFilter() {
  const select = document.getElementById("saved-select");
  const id = select.selectedOptions[0].dataset.id;
  const name = select.selectedOptions[0].textContent;
  if (!id || !confirm(`¿Borrar el filtro «${name}»?`)) return;
  const root = document.body.dataset.root || "";
  postFilter(`${root}/screener/filters/${id}/delete`, new URLSearchParams());
}
// Al tocar cualquier filtro, el selector deja de indicar un filtro guardado
document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("filters");
  const select = document.getElementById("saved-select");
  if (!form || !select) return;
  form.addEventListener("change", (e) => {
    if (e.target.closest(".saved-box")) return;
    select.selectedIndex = 0;
    document.getElementById("saved-delete").hidden = true;
  });
});
