/* Review page — save, autocomplete, checkbox logic, reprocess */
(function () {
  const saveBtn        = document.getElementById("save-btn");
  const saveStatus     = document.getElementById("save-status");
  const confirmAllBtn  = document.getElementById("confirm-all-btn");
  const skipLowBtn     = document.getElementById("skip-unmatched-btn");
  const toggleRawBtn   = document.getElementById("toggle-raw-btn");
  const rawText        = document.getElementById("raw-text");
  const sessionComment = document.getElementById("session-comment");
  const requestReprocess = document.getElementById("request-reprocess");
  const reprocessBtn   = document.getElementById("reprocess-btn");
  const reprocessStatus = document.getElementById("reprocess-status");

  if (toggleRawBtn && rawText) {
    toggleRawBtn.addEventListener("click", () => {
      const hidden = rawText.classList.toggle("hidden");
      toggleRawBtn.textContent = hidden ? "Show Extracted Text" : "Hide Extracted Text";
    });
  }

  function collectItems() {
    const rows = document.querySelectorAll(".item-row");
    const items = [];
    rows.forEach((row) => {
      const id       = parseInt(row.dataset.id, 10);
      const qty      = row.querySelector(".qty-input").value;
      const code     = row.querySelector(".item-code-hidden").value;
      const skipped  = row.querySelector(".skip-cb").checked;
      const confirmed = row.querySelector(".confirm-cb").checked;
      const comment = row.querySelector(".item-comment")?.value?.trim() || "";
      items.push({ id, quantity: parseFloat(qty) || 1, item_code: code, skipped, confirmed, comment });
    });
    return items;
  }

  // Save current edits and return a promise resolving to the response JSON
  function doSave(requestReprocessFlag) {
    const payload = {
      items: collectItems(),
      session_comment: sessionComment ? sessionComment.value.trim() : "",
      request_reprocess: requestReprocessFlag,
    };
    return fetch(window.SAVE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then((r) => r.json());
  }

  saveBtn.addEventListener("click", () => {
    saveBtn.disabled = true;
    saveStatus.textContent = "Saving…";
    doSave(requestReprocess ? requestReprocess.checked : false)
      .then((data) => {
        if (data.ok) {
          saveStatus.textContent = "Saved!";
          setTimeout(() => (saveStatus.textContent = ""), 2500);
        } else {
          saveStatus.textContent = "Save failed.";
        }
      })
      .catch(() => { saveStatus.textContent = "Network error."; })
      .finally(() => { saveBtn.disabled = false; });
  });

  // "Save & Reprocess" button — saves then runs the reprocess endpoint
  if (reprocessBtn) {
    reprocessBtn.addEventListener("click", () => {
      reprocessBtn.disabled = true;
      reprocessStatus.textContent = "Saving changes…";
      reprocessStatus.classList.remove("hidden");

      doSave(true)
        .then((saveData) => {
          if (!saveData.ok) throw new Error("Save failed");
          reprocessStatus.textContent = "Re-running item matching with your feedback…";
          return fetch(window.REPROCESS_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
          });
        })
        .then((r) => r.json())
        .then((data) => {
          if (!data.ok) throw new Error(data.error || "Reprocess failed");
          reprocessStatus.textContent = "Done! Reloading updated results…";
          window.location.href = data.redirect || window.location.href;
        })
        .catch((err) => {
          reprocessStatus.textContent = "Error: " + err.message;
          reprocessBtn.disabled = false;
        });
    });
  }

  if (confirmAllBtn) {
    confirmAllBtn.addEventListener("click", () => {
      document.querySelectorAll(".confirm-cb").forEach((cb) => { cb.checked = true; });
    });
  }

  if (skipLowBtn) {
    skipLowBtn.addEventListener("click", () => {
      document.querySelectorAll(".item-row.low-confidence").forEach((row) => {
        const cb = row.querySelector(".skip-cb");
        if (cb) { cb.checked = true; cb.dispatchEvent(new Event("change")); }
      });
    });
  }

  document.querySelectorAll(".skip-cb").forEach((cb) => {
    cb.addEventListener("change", () => {
      const row = cb.closest(".item-row");
      row.classList.toggle("row-skipped", cb.checked);
    });
  });

  let debounceTimer = null;

  function buildDropdown(dropdown, results, row) {
    dropdown.innerHTML = "";
    if (!results.length) {
      dropdown.innerHTML = '<div class="autocomplete-item" style="color:#6b7280">No matches found</div>';
      dropdown.classList.remove("hidden");
      return;
    }
    results.forEach((item) => {
      const div = document.createElement("div");
      div.className = "autocomplete-item";
      const conf = item.confidence_score != null
        ? ` <span style="color:#9ca3af;font-size:0.8em">${Math.round(item.confidence_score * 100)}%</span>`
        : "";
      div.innerHTML = `<span class="ac-code">${item.item_code}</span><span class="ac-desc">${item.description}</span>${conf}`;
      div.addEventListener("mousedown", (e) => {
        e.preventDefault();
        selectItem(row, item);
        dropdown.classList.add("hidden");
      });
      dropdown.appendChild(div);
    });
    dropdown.classList.remove("hidden");
  }

  function selectItem(row, item) {
    const descInput = row.querySelector(".desc-input");
    const codeHidden = row.querySelector(".item-code-hidden");
    const codeDisplay = row.querySelector(".item-code-display");
    descInput.value = item.description;
    codeHidden.value = item.item_code;
    codeDisplay.textContent = item.item_code;
    descInput.dataset.currentCode = item.item_code;
  }

  // Parse stored candidates from data-candidates attribute on the row
  function getCandidates(row) {
    try {
      const raw = row.dataset.candidates;
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      // Normalize: matcher returns {sku, description, confidence_score, ...}
      // api_erp_items returns {item_code, description, ...}
      return parsed.map((c) => ({
        item_code: c.item_code || c.sku || "",
        description: c.description || "",
        confidence_score: c.confidence_score,
      }));
    } catch (_) {
      return [];
    }
  }

  document.querySelectorAll(".desc-input").forEach((input) => {
    const row = input.closest(".item-row");
    const dropdown = row.querySelector(".autocomplete-dropdown");

    input.addEventListener("focus", () => {
      const q = input.value.trim();
      // First try to show stored candidates so the user sees top matches immediately
      const stored = getCandidates(row);
      if (stored.length) {
        buildDropdown(dropdown, stored, row);
      } else if (q) {
        triggerSearch(q, dropdown, row);
      }
    });

    input.addEventListener("input", () => {
      clearTimeout(debounceTimer);
      const q = input.value.trim();
      if (!q) { dropdown.classList.add("hidden"); return; }
      debounceTimer = setTimeout(() => triggerSearch(q, dropdown, row), 250);
    });

    input.addEventListener("blur", () => {
      setTimeout(() => dropdown.classList.add("hidden"), 150);
    });

    input.addEventListener("keydown", (e) => {
      const items = dropdown.querySelectorAll(".autocomplete-item");
      const focused = dropdown.querySelector(".autocomplete-item.focused");
      let idx = Array.from(items).indexOf(focused);

      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (focused) focused.classList.remove("focused");
        idx = (idx + 1) % items.length;
        items[idx] && items[idx].classList.add("focused");
        items[idx] && items[idx].scrollIntoView({ block: "nearest" });
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (focused) focused.classList.remove("focused");
        idx = idx <= 0 ? items.length - 1 : idx - 1;
        items[idx] && items[idx].classList.add("focused");
        items[idx] && items[idx].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (focused) focused.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      } else if (e.key === "Escape") {
        dropdown.classList.add("hidden");
      }
    });
  });

  function triggerSearch(q, dropdown, row) {
    const sessionId = window.SESSION_ID || "";
    fetch(`${window.ERP_SEARCH_URL}?q=${encodeURIComponent(q)}&session_id=${sessionId}`)
      .then((r) => r.json())
      .then((data) => buildDropdown(dropdown, data, row))
      .catch(() => dropdown.classList.add("hidden"));
  }
}());
