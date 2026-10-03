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
