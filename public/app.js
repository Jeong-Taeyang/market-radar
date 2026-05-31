/* ════════════════════════════════════════════════
   마켓레이더 — 메인 앱 로직
   ════════════════════════════════════════════════ */

const DATA = "data";

const PERSONAS = [
  { key: "bull",    label: "🐂 강세론자", short: "🐂" },
  { key: "bear",    label: "🐻 약세론자", short: "🐻" },
  { key: "quant",   label: "📐 퀀트",     short: "📐" },
  { key: "buffett", label: "🎩 버핏",     short: "🎩" },
];

// 목록 카드에 보여줄 미니 지표 4개
const MINI_KEYS = ["sp500", "kospi", "wti", "usd_krw"];
const MINI_LABELS = {
  sp500: "S&P", kospi: "KOSPI", wti: "WTI", usd_krw: "원/달러",
};

// 시장 그리드 지표 순서 + 라벨
const MARKET_KEYS = [
  { key: "sp500",   label: "S&P 500" },
  { key: "kospi",   label: "KOSPI" },
  { key: "usd_krw", label: "원/달러" },
  { key: "us10y",   label: "미국 10년물" },
  { key: "wti",     label: "WTI" },
  { key: "gold",    label: "금" },
  { key: "vix",     label: "VIX" },
  { key: "dxy",     label: "달러인덱스" },
  { key: "eurusd",  label: "EUR/USD" },
  { key: "usdjpy",  label: "USD/JPY" },
];

// 바텀시트 인디케이터 (historical 있는 8개)
const SHEET_INDICATORS = ["sp500", "kospi", "usd_krw", "us10y", "wti", "gold", "vix", "dxy"];

// ── 현재 상태 ──
let currentReport = null;
let currentPersona = "bull";
let currentSheetKey = "sp500";

// ════════════════════════════════════
// 유틸
// ════════════════════════════════════

function dirClass(changeStr) {
  if (!changeStr) return "flat";
  const n = parseFloat(changeStr);
  if (n > 0) return "up";
  if (n < 0) return "down";
  return "flat";
}

function dirColorClass(changeStr) {
  const d = dirClass(changeStr);
  return d === "up" ? "up-color" : d === "down" ? "down-color" : "flat-color";
}

function arrow(changeStr) {
  const d = dirClass(changeStr);
  return d === "up" ? "▲" : d === "down" ? "▼" : "━";
}

function formatDate(dateStr) {
  const [y, m, d] = dateStr.split("-");
  return `${y}년 ${parseInt(m)}월 ${parseInt(d)}일`;
}

// ════════════════════════════════════
// 목록 뷰
// ════════════════════════════════════

async function loadReports() {
  try {
    const res = await fetch(`${DATA}/reports.json`);
    if (!res.ok) throw new Error(res.status);
    const json = await res.json();
    renderList(json.reports || []);
  } catch (e) {
    console.error("reports.json 로드 실패:", e);
    document.getElementById("report-list").innerHTML = "";
    document.getElementById("empty-state").classList.remove("hidden");
  }
}

function renderList(reports) {
  const el = document.getElementById("report-list");
  if (!reports.length) {
    el.innerHTML = "";
    document.getElementById("empty-state").classList.remove("hidden");
    return;
  }

  el.innerHTML = reports.map((r, i) => {
    const mini = MINI_KEYS.map(k => {
      const q = r.market_data?.[k];
      if (!q) return `<div class="mini-indicator"><span class="mini-label">${MINI_LABELS[k]}</span><span class="mini-value flat-color">—</span></div>`;
      const dir = dirColorClass(q.change);
      return `
        <div class="mini-indicator">
          <span class="mini-label">${MINI_LABELS[k]}</span>
          <span class="mini-value ${dir}">${q.value}</span>
          <span class="mini-change ${dir}">${arrow(q.change)} ${q.change}</span>
        </div>`;
    }).join("");

    const badges = PERSONAS
      .filter(p => r[`has_${p.key}`])
      .map(p => `<span class="persona-badge">${p.short} ${p.label.split(" ")[1]}</span>`)
      .join("");

    return `
      <div class="report-card" data-date="${r.date}"
           style="animation-delay:${i * 50}ms" onclick="openDetail('${r.date}')">
        <div class="card-top">
          <span class="card-date">${formatDate(r.date)}</span>
        </div>
        <p class="card-title">${escapeHtml(r.title)}</p>
        <div class="card-market">${mini}</div>
        <div class="persona-badges">${badges}</div>
      </div>`;
  }).join("");
}

// ════════════════════════════════════
// 상세 뷰
// ════════════════════════════════════

async function openDetail(date) {
  // 목록 → 상세 전환
  document.getElementById("list-view").classList.add("hidden");
  const detail = document.getElementById("detail-view");
  detail.classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });

  // 스켈레톤 표시
  document.getElementById("detail-date").textContent = "";
  document.getElementById("detail-title").textContent = "불러오는 중...";
  document.getElementById("market-grid").innerHTML = "";
  document.getElementById("persona-content").innerHTML =
    '<div class="skeleton-text"></div><div class="skeleton-text short"></div><div class="skeleton-text"></div>';

  try {
    const res = await fetch(`${DATA}/${date}.json`);
    if (!res.ok) throw new Error(res.status);
    const report = await res.json();
    currentReport = report;
    currentPersona = "bull";
    renderDetail(report);
  } catch (e) {
    console.error("리포트 로드 실패:", e);
    document.getElementById("detail-title").textContent = "데이터를 불러올 수 없습니다.";
  }
}

function renderDetail(report) {
  document.getElementById("detail-date").textContent = formatDate(report.date);
  document.getElementById("detail-title").textContent = report.title;

  const sourceEl = document.getElementById("detail-source");
  if (report.source_url && report.source_url !== "https://www.kcif.or.kr/front/board/listBoardMsg.do?boardId=73") {
    sourceEl.href = report.source_url;
    sourceEl.style.display = "";
  } else {
    sourceEl.style.display = "none";
  }

  // 시장 그리드
  const grid = document.getElementById("market-grid");
  grid.innerHTML = MARKET_KEYS.map(({ key, label }) => {
    const q = report.market_data?.[key];
    if (!q) return "";
    const dir = dirClass(q.change);
    const onlyInSheet = SHEET_INDICATORS.includes(key);
    return `
      <div class="market-card ${dir}"
           ${onlyInSheet ? `onclick="openSheet('${key}', '${label}', '${report.date}')"` : ""}
           style="${onlyInSheet ? "cursor:pointer" : "cursor:default"}">
        <div class="market-card-label">${label}</div>
        <div class="market-card-value">${q.value}</div>
        <div class="market-card-change">${arrow(q.change)} ${q.change}</div>
      </div>`;
  }).join("");

  // 페르소나 탭
  const tabs = document.getElementById("persona-tabs");
  tabs.innerHTML = PERSONAS.map(p => `
    <button class="ptab ${p.key} ${p.key === currentPersona ? "active" : ""}"
            onclick="switchPersona('${p.key}')">
      ${p.short} ${p.label.split(" ")[1]}
    </button>`).join("");

  renderPersonaContent();
}

function switchPersona(key) {
  currentPersona = key;
  document.querySelectorAll(".ptab").forEach(el => {
    el.classList.toggle("active", el.classList.contains(key));
  });
  renderPersonaContent();
}

function renderPersonaContent() {
  const el = document.getElementById("persona-content");
  const text = currentReport?.[currentPersona];
  el.textContent = text || "⚠️ 이 페르소나 분석이 없습니다.";
}

// 뒤로가기
document.getElementById("back-btn").addEventListener("click", () => {
  document.getElementById("detail-view").classList.add("hidden");
  document.getElementById("list-view").classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
});

// ════════════════════════════════════
// 바텀시트
// ════════════════════════════════════

const overlay = document.getElementById("sheet-overlay");
const sheet   = document.getElementById("sheet");
let sheetOpen = false;
let sheetDate = null;

function openSheet(key, label, date) {
  currentSheetKey = key;
  sheetDate = date;

  // 도트 렌더
  const dots = document.getElementById("sheet-dots");
  dots.innerHTML = SHEET_INDICATORS.map(k => {
    const labels = { sp500:"S&P 500", kospi:"KOSPI", usd_krw:"원/달러", us10y:"미국 10년물", wti:"WTI", gold:"금", vix:"VIX", dxy:"DXY" };
    return `<span class="sdot ${k === key ? "active" : ""}"
                  onclick="switchSheetIndicator('${k}', '${labels[k]}')"
                  data-key="${k}">${labels[k]}</span>`;
  }).join("");

  // 현재 값 표시
  updateSheetHeader(key, label);

  // 기간 탭 초기화
  document.querySelectorAll(".period-tab").forEach(t => {
    t.classList.toggle("active", t.dataset.p === "1Y");
  });

  overlay.classList.add("visible");
  sheet.classList.add("open");
  sheetOpen = true;

  loadSheetChart(key, "1Y");
}

function updateSheetHeader(key, label) {
  const q = currentReport?.market_data?.[key];
  document.getElementById("sheet-label").textContent = label;
  if (q) {
    document.getElementById("sheet-value").textContent = q.value;
    const chEl = document.getElementById("sheet-change");
    chEl.textContent = `${arrow(q.change)} ${q.change}`;
    chEl.className = `sheet-change ${dirClass(q.change)}`;
  } else {
    document.getElementById("sheet-value").textContent = "—";
    document.getElementById("sheet-change").textContent = "";
  }
}

function switchSheetIndicator(key, label) {
  currentSheetKey = key;
  document.querySelectorAll(".sdot").forEach(d => {
    d.classList.toggle("active", d.dataset.key === key);
  });
  updateSheetHeader(key, label);
  const activePeriod = document.querySelector(".period-tab.active")?.dataset.p || "1Y";
  loadSheetChart(key, activePeriod);
}

function closeSheet() {
  overlay.classList.remove("visible");
  sheet.classList.remove("open");
  sheetOpen = false;
}

overlay.addEventListener("click", closeSheet);

// 기간 탭 클릭
document.querySelectorAll(".period-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".period-tab").forEach(t => t.classList.remove("active"));
    btn.classList.add("active");
    loadSheetChart(currentSheetKey, btn.dataset.p);
  });
});

// 스와이프로 닫기
let touchStartY = 0;
sheet.addEventListener("touchstart", e => { touchStartY = e.touches[0].clientY; }, { passive: true });
sheet.addEventListener("touchend", e => {
  if (e.changedTouches[0].clientY - touchStartY > 80) closeSheet();
}, { passive: true });

// ════════════════════════════════════
// HTML 이스케이프
// ════════════════════════════════════

function escapeHtml(str) {
  return str.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// ════════════════════════════════════
// 초기화
// ════════════════════════════════════

loadReports();
