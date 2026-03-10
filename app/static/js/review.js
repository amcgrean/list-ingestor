/* Review page — save, autocomplete, checkbox logic */
(function () {
  const saveBtn        = document.getElementById("save-btn");
  const saveStatus     = document.getElementById("save-status");
  const confirmAllBtn  = document.getElementById("confirm-all-btn");
  const skipLowBtn     = document.getElementById("skip-unmatched-btn");
  const toggleRawBtn   = document.getElementById("toggle-raw-btn");
  const rawText        = document.getElementById("raw-text");

  // ----------------------------------------------------------------
  // Raw text toggle
  // ----------------------------------------------------------------
  if (toggleRawBtn && rawText) {
    toggleRawBtn.addEventListener("click", () => {
      const hidden = rawText.classList.toggle("hidden");
      toggleRawBtn.textContent = hidden ? "Show Raw OCR Text" : "Hide Raw OCR Text";
    });
  }

  // ----------------------------------------------------------------
  // Collect row state
  // ----------------------------------------------------------------
  function collectItems() {
    const rows = document.querySelectorAll(".item-row");
    const items = [];
    rows.forEach((row) => {
      const id       = parseInt(row.dataset.id, 10);
      const qty      = row.querySelector(".qty-input").value;
      const code     = row.querySelector(".item-code-hidden").value;
      const skipped  = row.querySelector(".skip-cb").checked;
      const confirmed = row.querySelector(".confirm-cb").checked;
      items.push({ id, quantity: parseFloat(qty) || 1, item_code: code, skipped, confirmed });
    });
    return items;
  }

  // ----------------------------------------------------------------
  // Save
  // ----------------------------------------------------------------
  saveBtn.addEventListener("click", () => {
    const payload = { items: collectItems() };
    saveBtn.disabled = true;
    saveStatus.textContent = "Saving…";

    fetch(window.SAVE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json())
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

  // ----------------------------------------------------------------
  // Confirm all
  // ----------------------------------------------------------------
  if (confirmAllBtn) {
    confirmAllBtn.addEventListener("click", () => {
      document.querySelectorAll(".confirm-cb").forEach((cb) => { cb.checked = true; });
    });
  }

  // ----------------------------------------------------------------
  // Skip low-confidence
  // ----------------------------------------------------------------
  if (skipLowBtn) {
    skipLowBtn.addEventListener("click", () => {
      document.querySelectorAll(".item-row.low-confidence").forEach((row) => {
        const cb = row.querySelector(".skip-cb");
        if (cb) { cb.checked = true; cb.dispatchEvent(new Event("change")); }
      });
    });
  }

  // ----------------------------------------------------------------
  // Skip checkbox toggles row style
  // ----------------------------------------------------------------
  document.querySelectorAll(".skip-cb").forEach((cb) => {
    cb.addEventListener("change", () => {
      const row = cb.closest(".item-row");
      row.classList.toggle("row-skipped", cb.checked);
    });
  });

  // ----------------------------------------------------------------
  // Autocomplete for ERP item search
  // ----------------------------------------------------------------
  let activeDropdown = null;
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
      div.innerHTML = `<span class="ac-code">${item.item_code}</span><span class="ac-desc">${item.description}</span>`;
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

  document.querySelectorAll(".desc-input").forEach((input) => {
    const row = input.closest(".item-row");
    const dropdown = row.querySelector(".autocomplete-dropdown");

    input.addEventListener("focus", () => {
      if (input.value.trim()) triggerSearch(input.value.trim(), dropdown, row);
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

    // Keyboard nav
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
    fetch(`${window.ERP_SEARCH_URL}?q=${encodeURIComponent(q)}`)
      .then((r) => r.json())
      .then((data) => buildDropdown(dropdown, data, row))
      .catch(() => dropdown.classList.add("hidden"));
  }
}());
