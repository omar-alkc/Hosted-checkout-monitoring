/**
 * Client-side table sorting: click header cycles desc → asc → restore original order.
 * Mark tables with class `data js-sortable`, headers with `th-sortable` + data-sort-col / data-sort-type.
 * Optional `data-sort-default="desc|asc"` on one header for initial sort.
 */
(function () {
  "use strict";

  function dataRows(tbody) {
    return Array.prototype.filter.call(tbody.rows, function (tr) {
      if (!tr.cells || tr.cells.length === 0) return false;
      var c0 = tr.cells[0];
      if (c0 && c0.getAttribute("colspan")) return false;
      return true;
    });
  }

  function parseValue(td, sortType) {
    var raw = td.getAttribute("data-sort-value");
    if (raw === null || raw === "") raw = null;
    if (raw !== null && raw !== "") {
      if (sortType === "number") {
        var n = parseFloat(String(raw).replace(/,/g, ""));
        return isNaN(n) ? 0 : n;
      }
      if (sortType === "date") {
        var t = Date.parse(String(raw));
        return isNaN(t) ? 0 : t;
      }
      return String(raw).toLowerCase();
    }
    var text = (td.textContent || "").trim();
    if (sortType === "number") {
      var n2 = parseFloat(text.replace(/[^0-9.-]/g, ""));
      return isNaN(n2) ? 0 : n2;
    }
    if (sortType === "date") {
      var t2 = Date.parse(text);
      return isNaN(t2) ? 0 : t2;
    }
    return text.toLowerCase();
  }

  function sortTbody(tbody, colIndex, sortType, dir) {
    var rows = dataRows(tbody);
    rows.sort(function (a, b) {
      var ac = a.cells[colIndex];
      var bc = b.cells[colIndex];
      if (!ac || !bc) return 0;
      var av = parseValue(ac, sortType);
      var bv = parseValue(bc, sortType);
      var cmp = 0;
      if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
      else {
        if (av < bv) cmp = -1;
        else if (av > bv) cmp = 1;
      }
      return dir === "desc" ? -cmp : cmp;
    });
    rows.forEach(function (r) {
      tbody.appendChild(r);
    });
  }

  function clearMarkers(table) {
    table.querySelectorAll("th.th-sortable").forEach(function (th) {
      th.classList.remove("th-sort-desc", "th-sort-asc");
      th.removeAttribute("aria-sort");
    });
  }

  function setMarker(th, dir) {
    if (!th || !dir) return;
    th.classList.add(dir === "desc" ? "th-sort-desc" : "th-sort-asc");
    th.setAttribute("aria-sort", dir === "desc" ? "descending" : "ascending");
  }

  function initTable(table) {
    if (table.getAttribute("data-sort-inited") === "1") return;
    table.setAttribute("data-sort-inited", "1");

    var tbody = table.querySelector("tbody");
    if (!tbody) return;

    var baseline = dataRows(tbody).map(function (r) {
      return r;
    });

    var sortState = { col: null, dir: null };

    function applyBaseline() {
      baseline.forEach(function (r) {
        tbody.appendChild(r);
      });
    }

    table.querySelectorAll("th.th-sortable").forEach(function (th) {
      th.setAttribute("role", "button");
      th.setAttribute("tabindex", "0");
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          th.click();
        }
      });
      th.addEventListener("click", function () {
        var col = parseInt(th.getAttribute("data-sort-col"), 10);
        var typ = th.getAttribute("data-sort-type") || "text";
        if (isNaN(col)) return;

        var nextDir;
        if (sortState.col === col) {
          if (sortState.dir === "desc") nextDir = "asc";
          else if (sortState.dir === "asc") nextDir = null;
          else nextDir = "desc";
        } else {
          nextDir = "desc";
        }

        if (nextDir === null) {
          sortState = { col: null, dir: null };
          applyBaseline();
          clearMarkers(table);
          return;
        }

        sortState = { col: col, dir: nextDir };
        sortTbody(tbody, col, typ, nextDir);
        clearMarkers(table);
        setMarker(th, nextDir);
      });
    });

    var defTh = table.querySelector("th[data-sort-default]");
    if (defTh) {
      var dCol = parseInt(defTh.getAttribute("data-sort-col"), 10);
      var dTyp = defTh.getAttribute("data-sort-type") || "text";
      var dDir = (defTh.getAttribute("data-sort-default") || "desc").toLowerCase();
      if (dDir !== "asc" && dDir !== "desc") dDir = "desc";
      if (!isNaN(dCol)) {
        sortTbody(tbody, dCol, dTyp, dDir);
        sortState = { col: dCol, dir: dDir };
        clearMarkers(table);
        setMarker(defTh, dDir);
      }
    }
  }

  function boot() {
    document.querySelectorAll("table.data.js-sortable").forEach(initTable);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
