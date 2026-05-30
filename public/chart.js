/* ════════════════════════════════════════════════
   마켓레이더 — 바텀시트 Chart.js 모듈
   ════════════════════════════════════════════════ */

let chartInstance = null;

async function loadSheetChart(key, period) {
  const statsEl = document.getElementById("sheet-stats");
  statsEl.innerHTML = '<span class="stat-label" style="grid-column:1/-1">로딩 중...</span>';

  try {
    const res = await fetch(`data/historical/${key}.json`);
    if (!res.ok) throw new Error(res.status);
    const hist = await res.json();
    const filtered = filterByPeriod(hist.data, period);
    if (!filtered.length) throw new Error("데이터 없음");

    renderChart(filtered, hist.prefix || "", hist.decimals || 2, period);
    renderStats(filtered, hist.prefix || "", hist.decimals || 2);
  } catch (e) {
    console.warn("차트 로드 실패:", key, e);
    statsEl.innerHTML = '<span class="stat-label" style="grid-column:1/-1;color:#F04452">데이터를 불러올 수 없습니다.</span>';
    if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
  }
}

// ── 기간 필터 ──────────────────────────────────────
function filterByPeriod(data, period) {
  if (!data?.length) return [];
  const last = new Date(data[data.length - 1].d);

  if (period === "ALL") return data;

  let from;
  if (period === "1M") {
    from = new Date(last); from.setMonth(from.getMonth() - 1);
  } else if (period === "YTD") {
    from = new Date(last.getFullYear(), 0, 1);
  } else if (period === "1Y") {
    from = new Date(last); from.setFullYear(from.getFullYear() - 1);
  } else if (period === "5Y") {
    from = new Date(last); from.setFullYear(from.getFullYear() - 5);
  } else {
    return data;
  }

  const fromStr = from.toISOString().slice(0, 10);
  return data.filter(d => d.d >= fromStr);
}

// ── 차트 렌더 ───────────────────────────────────────
function renderChart(data, prefix, decimals, period) {
  const canvas = document.getElementById("sheet-chart");
  const ctx    = canvas.getContext("2d");

  const first = data[0].v;
  const last  = data[data.length - 1].v;
  const isUp  = last >= first;
  const color = isUp ? "#F04452" : "#3182F6";
  const fillColor = isUp
    ? "rgba(240, 68, 82, 0.08)"
    : "rgba(49, 130, 246, 0.08)";

  const labels = data.map(d => d.d);
  const values = data.map(d => d.v);

  if (chartInstance) chartInstance.destroy();

  chartInstance = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: color,
        borderWidth: 2,
        fill: true,
        backgroundColor: fillColor,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHoverBackgroundColor: color,
        tension: 0.2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#191F28",
          titleColor: "#8B95A1",
          bodyColor: "#FFFFFF",
          padding: 10,
          cornerRadius: 8,
          callbacks: {
            title: items => items[0].label.replace("T00:00:00.000Z", "").split("T")[0],
            label: item => {
              const v = item.raw;
              return ` ${prefix}${v.toLocaleString("ko-KR", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}`;
            },
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          border: { display: false },
          ticks: {
            color: "#8B95A1",
            font: { size: 10 },
            maxTicksLimit: 6,
            maxRotation: 0,
            callback(val, idx) {
              const d = new Date(this.getLabelForValue(val));
              if (period === "1M") return `${d.getMonth()+1}/${d.getDate()}`;
              if (period === "YTD" || period === "1Y") {
                return `${d.getMonth()+1}월`;
              }
              return `${d.getFullYear()}`;
            },
          },
        },
        y: {
          position: "right",
          grid: { color: "#F2F4F6" },
          border: { display: false, dash: [4, 4] },
          ticks: {
            color: "#8B95A1",
            font: { size: 10 },
            maxTicksLimit: 5,
            callback(v) {
              if (v >= 10000) return `${(v / 1000).toFixed(0)}K`;
              return `${prefix}${v.toLocaleString("ko-KR", { maximumFractionDigits: decimals <= 2 ? decimals : 2 })}`;
            },
          },
        },
      },
    },
  });
}

// ── 통계 렌더 ───────────────────────────────────────
function renderStats(data, prefix, decimals) {
  const values = data.map(d => d.v);
  const high   = Math.max(...values);
  const low    = Math.min(...values);
  const first  = data[0].v;
  const last   = data[data.length - 1].v;
  const ret    = ((last - first) / first) * 100;
  const sign   = ret >= 0 ? "+" : "";
  const retColor = ret >= 0 ? "var(--up)" : "var(--down)";

  const fmt = v => `${prefix}${v.toLocaleString("ko-KR", { minimumFractionDigits: Math.min(decimals, 2), maximumFractionDigits: Math.min(decimals, 2) })}`;

  document.getElementById("sheet-stats").innerHTML = `
    <div class="stat-item">
      <span class="stat-label">기간 고가</span>
      <span class="stat-value">${fmt(high)}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">기간 저가</span>
      <span class="stat-value">${fmt(low)}</span>
    </div>
    <div class="stat-item">
      <span class="stat-label">기간 수익률</span>
      <span class="stat-value" style="color:${retColor}">${sign}${ret.toFixed(2)}%</span>
    </div>`;
}
