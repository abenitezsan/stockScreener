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
