// Gráficos de la ficha de un valor. Una serie por gráfico más líneas de referencia rotuladas.
(function () {
  const data = JSON.parse(document.getElementById("chart-data").textContent);
  const css = getComputedStyle(document.documentElement);
  const token = (name) => css.getPropertyValue(name).trim();
  const color = {
    series: token("--series-1"),
    fair: token("--ref-fair"),
    buy: token("--status-good"),
    grid: token("--grid"),
    muted: token("--ink-muted"),
  };
  const fmt = (v, digits = 2) =>
    v == null ? "–" : v.toLocaleString("es-ES", { minimumFractionDigits: digits, maximumFractionDigits: digits });

  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.color = color.muted;

  function reference(label, value, stroke, n) {
    return {
      label, data: Array(n).fill(value), borderColor: stroke, borderWidth: 1.5,
      borderDash: [6, 4], pointRadius: 0, pointHoverRadius: 0, fill: false,
    };
  }

  function options(suffix, digits, showLegend) {
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: showLegend, position: "bottom", labels: { usePointStyle: true, pointStyle: "line", boxWidth: 24 } },
        tooltip: { callbacks: { label: (ctx) => ` ${fmt(ctx.parsed.y, digits)}${suffix}  ${ctx.dataset.label}` } },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            maxTicksLimit: 5, maxRotation: 0,
            // Fechas ISO -> "01/2025"; los años del gráfico de barras quedan igual
            callback(value) {
              const label = this.getLabelForValue(value);
              return label.length === 10 ? `${label.slice(5, 7)}/${label.slice(0, 4)}` : label;
            },
          },
        },
        y: { grid: { color: color.grid }, border: { display: false }, ticks: { callback: (v) => fmt(v, digits) + suffix } },
      },
    };
  }

  function line(id, points, label, refs, suffix, digits) {
    const el = document.getElementById(id);
    if (!points.length) { el.replaceWith(Object.assign(document.createElement("p"), { textContent: "Sin datos", className: "muted" })); return; }
    const labels = points.map((p) => p[0]);
    const datasets = [{
      label, data: points.map((p) => p[1]), borderColor: color.series, borderWidth: 2,
      pointRadius: 0, pointHoverRadius: 4, tension: 0,
    }];
    for (const [refLabel, value, stroke] of refs) {
      if (value != null) datasets.push(reference(refLabel, value, stroke, points.length));
    }
    new Chart(el, { type: "line", data: { labels, datasets }, options: options(suffix, digits, datasets.length > 1) });
  }

  line("chart-price", data.price, "Precio", [
    ["Precio justo", data.fair_value, color.fair],
    ["Precio de compra", data.buy_price, color.buy],
  ], "", 2);

  line("chart-yield", data.yield, "Yield", [
    ["Media 5 años", data.yield_avg, color.fair],
    ["Percentil 80 (barata)", data.yield_p80, color.buy],
  ], " %", 2);

  const divEl = document.getElementById("chart-div");
  if (!data.annual.length) {
    divEl.replaceWith(Object.assign(document.createElement("p"), { textContent: "Sin datos", className: "muted" }));
  } else {
    const opts = options("", 2, false);
    opts.interaction = { mode: "nearest", intersect: true };
    new Chart(divEl, {
      type: "bar",
      data: {
        labels: data.annual.map((p) => p[0]),
        datasets: [{
          label: "Dividendo", data: data.annual.map((p) => p[1]), backgroundColor: color.series,
          borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", maxBarThickness: 28,
        }],
      },
      options: opts,
    });
  }
})();
