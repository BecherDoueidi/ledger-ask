/*
 * Ask-a-question page: renders each question/answer as a "turn" appended
 * to a scrollable transcript (rather than replacing a single result area)
 * so the multi-turn conversation the backend now supports (see
 * conversation_state.py / followup_resolver.py) is actually visible as a
 * conversation, not just a series of unrelated flashes of a single panel.
 *
 * Each turn owns its own DataTable instance (sort/search/paginate are
 * pure client-side view conveniences over the rows already returned --
 * they never call the API and never affect conversation_state; typing
 * a natural-language follow-up like "sort them by name" is the separate,
 * server-side, conversation-aware path). Chart.js instances are tracked
 * per-turn so switching pages/turns doesn't leak canvases.
 */

(function () {
  const { toast, apiFetch, copyToClipboard } = window.LedgerAsk;

  const form = document.getElementById("ask-form");
  const questionInput = document.getElementById("question");
  const askBtn = document.getElementById("ask-btn");
  const transcript = document.getElementById("transcript");
  const emptyState = document.getElementById("empty-state");
  const newQuestionBtn = document.getElementById("new-question-btn");

  const chartInstances = [];

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str ?? "";
    return div.innerHTML;
  }

  function fillExample(text) {
    questionInput.value = text;
    questionInput.focus();
  }
  window.fillExample = fillExample;

  // ---------------------------------------------------------------
  // DataTable: sort / search / paginate a fixed row array client-side.
  // ---------------------------------------------------------------
  function createDataTable(rows) {
    const PAGE_SIZE = 10;
    let state = { sortCol: null, sortDir: 1, search: "", page: 1 };
    const columns = rows.length ? Object.keys(rows[0]) : [];

    function filteredSorted() {
      let out = rows;
      if (state.search) {
        const q = state.search.toLowerCase();
        out = out.filter((row) =>
          columns.some((c) => String(row[c] ?? "").toLowerCase().includes(q))
        );
      }
      if (state.sortCol) {
        out = [...out].sort((a, b) => {
          const av = a[state.sortCol], bv = b[state.sortCol];
          if (av === bv) return 0;
          if (av === null || av === undefined) return 1;
          if (bv === null || bv === undefined) return -1;
          return (av > bv ? 1 : -1) * state.sortDir;
        });
      }
      return out;
    }

    function render(container) {
      const data = filteredSorted();
      const totalPages = Math.max(1, Math.ceil(data.length / PAGE_SIZE));
      state.page = Math.min(state.page, totalPages);
      const pageRows = data.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE);

      if (rows.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="empty-title">No rows returned</div></div>';
        return;
      }

      // The search box re-renders the whole toolbar+table on every
      // keystroke (simplest way to keep search/sort/pagination in sync),
      // but replacing innerHTML destroys and recreates the <input> node
      // -- the browser drops focus on the old node and the new one never
      // gets it, so typing a second character required clicking back in
      // first. Capture focus + cursor position before the rebuild and
      // restore both on the new node afterward.
      const searchHadFocus = document.activeElement === container.querySelector('[data-role="search"]');
      const priorSelection = searchHadFocus
        ? [container.querySelector('[data-role="search"]').selectionStart,
           container.querySelector('[data-role="search"]').selectionEnd]
        : null;

      const toolbar = `
        <div class="table-toolbar">
          <div class="table-search">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input type="text" placeholder="Search rows…" value="${escapeHtml(state.search)}" data-role="search">
          </div>
          <div class="table-actions">
            <button type="button" class="btn btn-ghost btn-sm" data-role="copy-csv">Copy CSV</button>
            <button type="button" class="btn btn-ghost btn-sm" data-role="download-csv">Download CSV</button>
            <button type="button" class="btn btn-ghost btn-sm" data-role="download-json">Export JSON</button>
          </div>
        </div>`;

      const thead = `<tr>${columns.map((c) => {
        const sorted = state.sortCol === c;
        const arrow = sorted ? (state.sortDir === 1 ? "▲" : "▼") : "▲";
        return `<th data-col="${escapeHtml(c)}" class="${sorted ? "sorted" : ""}">${escapeHtml(c)}<span class="sort-arrow">${arrow}</span></th>`;
      }).join("")}</tr>`;

      const tbody = pageRows.map((row) => `<tr>${columns.map((c) => {
        const v = row[c];
        return `<td>${v === null || v === undefined ? '<span class="null">NULL</span>' : escapeHtml(String(v))}</td>`;
      }).join("")}</tr>`).join("");

      const pagination = totalPages > 1 ? `
        <div class="table-pagination">
          <span>${data.length} row${data.length === 1 ? "" : "s"} · page ${state.page} of ${totalPages}</span>
          <div class="pages">
            <button type="button" class="btn btn-ghost btn-sm" data-role="prev-page" ${state.page <= 1 ? "disabled" : ""}>← Prev</button>
            <button type="button" class="btn btn-ghost btn-sm" data-role="next-page" ${state.page >= totalPages ? "disabled" : ""}>Next →</button>
          </div>
        </div>` : `<div class="table-pagination"><span>${data.length} row${data.length === 1 ? "" : "s"}</span></div>`;

      container.innerHTML = `
        ${toolbar}
        <div class="table-scroll"><table class="data-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table></div>
        ${pagination}
      `;

      const searchInput = container.querySelector('[data-role="search"]');
      searchInput.addEventListener("input", (e) => {
        state.search = e.target.value;
        state.page = 1;
        render(container);
      });
      if (searchHadFocus) {
        searchInput.focus();
        if (priorSelection) searchInput.setSelectionRange(priorSelection[0], priorSelection[1]);
      }
      container.querySelectorAll("th[data-col]").forEach((th) => {
        th.addEventListener("click", () => {
          const col = th.dataset.col;
          state.sortDir = state.sortCol === col ? -state.sortDir : 1;
          state.sortCol = col;
          render(container);
        });
      });
      const prevBtn = container.querySelector('[data-role="prev-page"]');
      const nextBtn = container.querySelector('[data-role="next-page"]');
      if (prevBtn) prevBtn.addEventListener("click", () => { state.page--; render(container); });
      if (nextBtn) nextBtn.addEventListener("click", () => { state.page++; render(container); });

      container.querySelector('[data-role="copy-csv"]').addEventListener("click", () => {
        copyToClipboard(toCsv(rows, columns), "Copied CSV to clipboard");
      });
      container.querySelector('[data-role="download-csv"]').addEventListener("click", () => {
        downloadFile(toCsv(rows, columns), "text/csv", "result.csv");
      });
      container.querySelector('[data-role="download-json"]').addEventListener("click", () => {
        downloadFile(JSON.stringify(rows, null, 2), "application/json", "result.json");
      });
    }

    return { render };
  }

  function toCsv(rows, columns) {
    const escapeCell = (v) => {
      const s = v === null || v === undefined ? "" : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    return [columns.join(","), ...rows.map((r) => columns.map((c) => escapeCell(r[c])).join(","))].join("\n");
  }

  function downloadFile(content, mime, filename) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
    toast(`Downloaded ${filename}`, "success");
  }

  // ---------------------------------------------------------------
  // Chart rendering
  // ---------------------------------------------------------------

  // Matches chart_advisor.py's PALETTE so a manually-switched chart type
  // stays visually consistent with what the backend would have chosen.
  const CHART_PALETTE = ["#C4621A", "#2C5282", "#2F6B4F", "#A33A2E", "#6B4C9A", "#1F7A8C", "#B8860B", "#7A5C3E"];

  // Only these three share a compatible data shape (labels[] + one
  // number per label) so a user can freely switch between them. Scatter
  // uses {x, y} point objects instead of a label+value pair and can't be
  // derived from the other shapes without picking which field becomes
  // the label -- rather than guess, scatter charts aren't switchable.
  const SWITCHABLE_TYPES = ["bar", "line", "pie"];

  function restyleDatasetsForType(datasets, chartType, labelCount) {
    return datasets.map((ds, i) => {
      const styled = { label: ds.label, data: ds.data };
      if (chartType === "pie") {
        styled.backgroundColor = Array.from({ length: labelCount }, (_, j) => CHART_PALETTE[j % CHART_PALETTE.length]);
      } else {
        styled.backgroundColor = CHART_PALETTE[i % CHART_PALETTE.length];
        if (chartType === "line") styled.borderColor = CHART_PALETTE[i % CHART_PALETTE.length];
      }
      return styled;
    });
  }

  function buildChartConfig(chartType, viz, datasets) {
    return {
      type: chartType,
      data: chartType === "scatter" ? { datasets } : { labels: viz.labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: datasets.length > 1 || chartType === "pie" },
          tooltip: { enabled: true },
        },
        scales: chartType === "pie" ? {} : {
          x: { title: { display: !!viz.x_label, text: viz.x_label || "" } },
          y: { title: { display: !!viz.y_label, text: viz.y_label || "" }, beginAtZero: true },
        },
      },
    };
  }

  function renderChart(container, viz) {
    if (!viz) return;
    const card = document.createElement("div");
    card.className = "card chart-card";
    const canvasId = `chart-${Date.now()}-${Math.floor(Math.random() * 10000)}`;

    // Pie only reads its first dataset in Chart.js, so offering it as a
    // switch target when the result has multiple measures (multi-series
    // bar/line) would silently drop data -- only offered for a single
    // dataset.
    const canSwitch = viz.chart_type !== "scatter";
    const availableTypes = SWITCHABLE_TYPES.filter((t) => t !== "pie" || viz.datasets.length === 1);
    const switcherHtml = canSwitch ? `
      <div class="chart-type-switch" role="group" aria-label="Chart type">
        ${availableTypes.map((t) => `
          <button type="button" class="chart-type-btn${t === viz.chart_type ? " active" : ""}" data-chart-type="${t}">${t.charAt(0).toUpperCase() + t.slice(1)}</button>
        `).join("")}
      </div>` : "";

    card.innerHTML = `
      <div class="chart-card-header">
        <h3>${escapeHtml(viz.title || "Visualization")}</h3>
        ${switcherHtml}
      </div>
      <div class="chart-canvas-wrap"><canvas id="${canvasId}"></canvas></div>
    `;
    container.appendChild(card);

    const canvas = card.querySelector("canvas");
    let currentType = viz.chart_type;
    let chart = new Chart(canvas, buildChartConfig(currentType, viz, viz.datasets));
    chartInstances.push(chart);

    if (canSwitch) {
      card.querySelectorAll(".chart-type-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          const newType = btn.dataset.chartType;
          if (newType === currentType) return;

          const slot = chartInstances.indexOf(chart);
          chart.destroy();
          const restyled = restyleDatasetsForType(viz.datasets, newType, (viz.labels || []).length);
          chart = new Chart(canvas, buildChartConfig(newType, viz, restyled));
          if (slot !== -1) chartInstances[slot] = chart;
          else chartInstances.push(chart);

          currentType = newType;
          card.querySelectorAll(".chart-type-btn").forEach((b) => b.classList.toggle("active", b.dataset.chartType === newType));
        });
      });
    }
  }

  function destroyAllCharts() {
    chartInstances.splice(0).forEach((c) => c.destroy());
  }

  // ---------------------------------------------------------------
  // Turn rendering (one question/answer pair in the transcript)
  // ---------------------------------------------------------------
  function sourceBadge(body) {
    if (body.source === "catalog") return '<span class="badge badge-warning">From catalog — no AI used</span>';
    if (body.source === "transform") return '<span class="badge badge-accent">Applied instantly — no query needed</span>';
    if (body.cached && body.match_type === "semantic") {
      const pct = Math.round((body.similarity || 0) * 100);
      return `<span class="badge badge-info">Instant — similar question seen before (${pct}% match, hit #${body.hit_count})</span>`;
    }
    if (body.cached) return `<span class="badge badge-success">Instant — seen before (hit #${body.hit_count})</span>`;
    return '<span class="badge badge-neutral">Freshly generated</span>';
  }

  function renderSkeletonTurn(question) {
    const turn = document.createElement("div");
    turn.className = "turn";
    turn.innerHTML = `
      <div class="turn-question"><div class="q-bubble">${escapeHtml(question)}</div></div>
      <div class="card turn-answer">
        <div class="skeleton-row"><div class="skeleton-block" style="height:22px;width:180px;"></div></div>
        <div class="skeleton-row"><div class="skeleton-block" style="height:40px;width:100%;"></div></div>
        <div class="skeleton-row"><div class="skeleton-block" style="height:16px;width:90%;"></div></div>
        <div class="skeleton-row"><div class="skeleton-block" style="height:16px;width:70%;"></div></div>
        <div class="skeleton-row"><div class="skeleton-block" style="height:16px;width:80%;"></div></div>
      </div>
    `;
    return turn;
  }

  function renderResultTurn(question, body) {
    const turn = document.createElement("div");
    turn.className = "turn";

    const questionHtml = `<div class="turn-question"><div class="q-bubble">${escapeHtml(question)}</div></div>`;
    const answer = document.createElement("div");
    answer.className = "card turn-answer";

    if (body.status !== "success") {
      answer.innerHTML = `<div class="message-box message-error">⚠️ ${escapeHtml(body.message || "Something went wrong.")}</div>`;
      turn.innerHTML = questionHtml;
      turn.appendChild(answer);
      return turn;
    }

    let html = `<div class="turn-meta">${sourceBadge(body)}</div>`;

    if (body.transform_log && body.transform_log.length) {
      html += `<div class="transform-trail"><strong>Applied to the current view:</strong> ${body.transform_log.map(escapeHtml).join(" → ")}</div>`;
    }

    if (body.generated_sql) {
      html += `<div class="code-block"><button class="copy-btn" data-role="copy-sql" type="button">Copy</button><pre>${escapeHtml(body.generated_sql)}</pre></div>`;
    }

    answer.innerHTML = html;

    if (body.generated_sql) {
      answer.querySelector('[data-role="copy-sql"]').addEventListener("click", () => {
        copyToClipboard(body.generated_sql, "Copied SQL to clipboard");
      });
    }

    if (body.data) {
      const tableContainer = document.createElement("div");
      answer.appendChild(tableContainer);
      createDataTable(body.data).render(tableContainer);
      if (body.visualization) {
        renderChart(answer, body.visualization);
      }
    } else if (body.message) {
      const msg = document.createElement("div");
      msg.className = "message-box message-success";
      msg.textContent = "✓ " + body.message;
      answer.appendChild(msg);
    }

    turn.innerHTML = questionHtml;
    turn.appendChild(answer);
    return turn;
  }

  function clearTranscriptUI() {
    transcript.innerHTML = "";
    destroyAllCharts();
    emptyState.style.display = "";
  }

  async function submitQuestion(question) {
    emptyState.style.display = "none";
    const skeleton = renderSkeletonTurn(question);
    transcript.appendChild(skeleton);
    skeleton.scrollIntoView({ behavior: "smooth", block: "end" });

    askBtn.disabled = true;
    askBtn.textContent = "Thinking…";

    try {
      const response = await apiFetch("/api/generate-sql", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: question }),
      });
      const body = await response.json();
      const turn = renderResultTurn(question, body);
      skeleton.replaceWith(turn);
      turn.scrollIntoView({ behavior: "smooth", block: "end" });
      if (body.status !== "success") toast(body.message || "Something went wrong", "error");
    } catch (err) {
      if (err.message === "Not authenticated") return;
      const turn = document.createElement("div");
      turn.className = "turn";
      turn.innerHTML = `<div class="turn-question"><div class="q-bubble">${escapeHtml(question)}</div></div>
        <div class="card turn-answer"><div class="message-box message-error">⚠️ Could not reach the server. Is app.py running?</div></div>`;
      skeleton.replaceWith(turn);
      toast("Could not reach the server", "error");
    } finally {
      askBtn.disabled = false;
      askBtn.textContent = "Ask";
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const question = questionInput.value.trim();
    if (!question) return;
    questionInput.value = "";
    submitQuestion(question);
  });

  newQuestionBtn.addEventListener("click", async () => {
    await apiFetch("/api/conversation/clear", { method: "POST" });
    clearTranscriptUI();
    questionInput.focus();
    toast("Started a new question", "success");
  });

  // Keyboard shortcuts: Ctrl/Cmd+Enter submits from anywhere in the
  // textarea (plain Enter already submits via the form's default
  // behavior being overridden below to allow multi-line input with
  // Shift+Enter); "/" focuses the input from anywhere else on the page.
  questionInput.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      form.requestSubmit();
    } else if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== questionInput) {
      e.preventDefault();
      questionInput.focus();
    }
  });

  questionInput.focus();
})();
