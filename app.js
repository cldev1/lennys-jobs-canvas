(() => {
  "use strict";

  const LEVEL_LABELS = {
    1: "Intern / Entry",
    2: "Junior",
    3: "Mid",
    4: "Senior",
    5: "Staff / Lead",
    6: "Principal+",
    7: "Director",
    8: "VP+",
    9: "C-level",
  };

  const state = {
    jobs: [],
    meta: null,
    filtered: [],
  };

  const el = {
    metaBar: document.getElementById("metaBar"),
    searchInput: document.getElementById("searchInput"),
    companyFilter: document.getElementById("companyFilter"),
    locationFilter: document.getElementById("locationFilter"),
    levelFilter: document.getElementById("levelFilter"),
    sortSelect: document.getElementById("sortSelect"),
    salaryOnly: document.getElementById("salaryOnly"),
    resetFilters: document.getElementById("resetFilters"),
    resultCount: document.getElementById("resultCount"),
    cards: document.getElementById("cards"),
    emptyState: document.getElementById("emptyState"),
  };

  function fmtMoney(n) {
    if (n == null || Number.isNaN(Number(n))) return null;
    const v = Number(n);
    if (v >= 1000) return `$${(v / 1000).toFixed(v % 1000 === 0 ? 0 : 0)}k`;
    return `$${v}`;
  }

  function salaryLabel(job) {
    const min = fmtMoney(job.salary_range_min);
    const max = fmtMoney(job.salary_range_max);
    if (min && max) return `${min}–${max}`;
    if (min) return `from ${min}`;
    if (max) return `up to ${max}`;
    return null;
  }

  function levelLabel(level) {
    if (level == null || level === "") return null;
    return LEVEL_LABELS[level] || `Level ${level}`;
  }

  function formatDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function uniqueSorted(values) {
    return [...new Set(values.filter(Boolean))].sort((a, b) =>
      String(a).localeCompare(String(b), undefined, { sensitivity: "base" })
    );
  }

  function fillSelect(select, values, allLabel) {
    const current = select.value;
    select.innerHTML = "";
    const all = document.createElement("option");
    all.value = "";
    all.textContent = allLabel;
    select.appendChild(all);
    for (const v of values) {
      const opt = document.createElement("option");
      opt.value = String(v);
      opt.textContent = String(v);
      select.appendChild(opt);
    }
    if ([...select.options].some((o) => o.value === current)) {
      select.value = current;
    }
  }

  function populateFilters() {
    fillSelect(
      el.companyFilter,
      uniqueSorted(state.jobs.map((j) => j.company_name)),
      "All companies"
    );
    fillSelect(
      el.locationFilter,
      uniqueSorted(state.jobs.map((j) => j.location)),
      "All locations"
    );
    const levels = uniqueSorted(state.jobs.map((j) => j.level)).sort((a, b) => Number(a) - Number(b));
    el.levelFilter.innerHTML = "";
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "All levels";
    el.levelFilter.appendChild(all);
    for (const lv of levels) {
      const opt = document.createElement("option");
      opt.value = String(lv);
      opt.textContent = levelLabel(lv) || String(lv);
      el.levelFilter.appendChild(opt);
    }
  }

  function matchesSearch(job, q) {
    if (!q) return true;
    const hay = [
      job.title,
      job.company_name,
      job.location,
      job.business_description_short,
      ...(job.description_tags || []),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return q.split(/\s+/).every((tok) => hay.includes(tok));
  }

  function applyFilters() {
    const q = el.searchInput.value.trim().toLowerCase();
    const company = el.companyFilter.value;
    const location = el.locationFilter.value;
    const level = el.levelFilter.value;
    const salaryOnly = el.salaryOnly.checked;
    const sort = el.sortSelect.value;

    let list = state.jobs.filter((j) => {
      if (company && j.company_name !== company) return false;
      if (location && j.location !== location) return false;
      if (level !== "" && String(j.level) !== level) return false;
      if (salaryOnly && j.salary_range_min == null && j.salary_range_max == null) return false;
      if (!matchesSearch(j, q)) return false;
      return true;
    });

    const sorters = {
      updated_desc: (a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")),
      salary_desc: (a, b) => (b.salary_range_max || b.salary_range_min || 0) - (a.salary_range_max || a.salary_range_min || 0),
      salary_asc: (a, b) => (a.salary_range_min || a.salary_range_max || 0) - (b.salary_range_min || b.salary_range_max || 0),
      title_asc: (a, b) => String(a.title || "").localeCompare(String(b.title || "")),
      company_asc: (a, b) => String(a.company_name || "").localeCompare(String(b.company_name || "")),
      trajectory_desc: (a, b) => (b.trajectory_score || 0) - (a.trajectory_score || 0),
    };
    list.sort(sorters[sort] || sorters.updated_desc);

    state.filtered = list;
    render();
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function cardHtml(job) {
    const salary = salaryLabel(job);
    const level = levelLabel(job.level);
    const tags = (job.description_tags || []).slice(0, 6);
    const desc = job.business_description_short
      ? `<p class="card-desc">${escapeHtml(job.business_description_short)}</p>`
      : "";
    const url = job.url || "#";
    const pills = [
      job.location ? `<span class="pill">${escapeHtml(job.location)}</span>` : "",
      level ? `<span class="pill">${escapeHtml(level)}</span>` : "",
      salary ? `<span class="pill salary">${escapeHtml(salary)}</span>` : "",
      job.trajectory_score != null
        ? `<span class="pill">traj ${Number(job.trajectory_score).toFixed(0)}</span>`
        : "",
    ]
      .filter(Boolean)
      .join("");

    return `
      <article class="card">
        <h3 class="card-title">${escapeHtml(job.title || "Untitled")}</h3>
        <p class="card-company">${escapeHtml(job.company_name || "Unknown")}</p>
        <div class="card-meta">${pills}</div>
        ${desc}
        ${
          tags.length
            ? `<div class="tags">${tags
                .map((t) => `<span class="tag">${escapeHtml(t)}</span>`)
                .join("")}</div>`
            : ""
        }
        <div class="card-actions">
          <a class="apply" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">Apply</a>
          <span class="updated">${escapeHtml(formatDate(job.updated_at))}</span>
        </div>
      </article>
    `;
  }

  function render() {
    const n = state.filtered.length;
    const total = state.jobs.length;
    el.resultCount.textContent =
      n === total ? `${n.toLocaleString()} jobs` : `${n.toLocaleString()} of ${total.toLocaleString()} jobs`;
    el.emptyState.hidden = n > 0;
    // Cap DOM for snappy UI; filters still apply to full set shown count.
    const slice = state.filtered.slice(0, 400);
    el.cards.innerHTML = slice.map(cardHtml).join("");
    if (n > 400) {
      el.cards.insertAdjacentHTML(
        "beforeend",
        `<p class="empty">Showing first 400 matches — refine search to narrow further.</p>`
      );
    }
  }

  function bind() {
    const rerun = () => applyFilters();
    el.searchInput.addEventListener("input", rerun);
    el.companyFilter.addEventListener("change", rerun);
    el.locationFilter.addEventListener("change", rerun);
    el.levelFilter.addEventListener("change", rerun);
    el.sortSelect.addEventListener("change", rerun);
    el.salaryOnly.addEventListener("change", rerun);
    el.resetFilters.addEventListener("click", () => {
      el.searchInput.value = "";
      el.companyFilter.value = "";
      el.locationFilter.value = "";
      el.levelFilter.value = "";
      el.sortSelect.value = "updated_desc";
      el.salaryOnly.checked = false;
      applyFilters();
    });
  }

  async function load() {
    try {
      const [jobsRes, metaRes] = await Promise.all([
        fetch("data/jobs.json"),
        fetch("data/meta.json"),
      ]);
      if (!jobsRes.ok) throw new Error(`jobs.json HTTP ${jobsRes.status}`);
      state.jobs = await jobsRes.json();
      state.meta = metaRes.ok ? await metaRes.json() : null;

      const m = state.meta || {};
      const when = m.fetched_at ? formatDate(m.fetched_at) : "unknown";
      el.metaBar.textContent = `${(m.count || state.jobs.length).toLocaleString()} jobs · query “${m.query || "—"}” · fetched ${when}`;

      populateFilters();
      bind();
      applyFilters();
    } catch (err) {
      el.metaBar.textContent = "Failed to load data/jobs.json";
      el.resultCount.textContent = "Error";
      el.cards.innerHTML = `<p class="empty">${escapeHtml(err.message)}. Run the fetcher and serve from the repo root.</p>`;
      console.error(err);
    }
  }

  load();
})();
