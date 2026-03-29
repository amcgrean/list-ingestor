/* Upload page interactions — async processing with status polling */
(function () {
  var dropZone    = document.getElementById("drop-zone");
  var fileInput   = document.getElementById("file-input");
  var cameraInput = document.getElementById("camera-input");
  var mobileFileBtn = document.getElementById("mobile-file-btn");
  var filePreview = document.getElementById("file-preview");
  var fileName    = document.getElementById("file-name");
  var fileCount   = document.getElementById("file-count");
  var clearBtn    = document.getElementById("clear-file");
  var submitBtn   = document.getElementById("submit-btn");
  var spinner     = document.getElementById("spinner");
  var form        = document.getElementById("upload-form");
  var processingCard = document.getElementById("processing-card");
  var processingMsg  = document.getElementById("processing-message");
  var processingErr  = document.getElementById("processing-error");

  function isAllowedFile(file) {
    var ext = (file.name.split(".").pop() || "").toLowerCase();
    var allowedExts = ["jpg", "jpeg", "png", "pdf", "webp", "csv"];
    if (allowedExts.indexOf(ext) !== -1) return true;
    return file.type.startsWith("image/") || file.type === "application/pdf" || file.type === "text/csv";
  }

  function setFiles(files) {
    if (!files || !files.length) return;
    var valid = Array.from(files).filter(isAllowedFile);
    if (!valid.length) {
      alert("Unsupported file type. Please upload images (including HEIC), PDFs, or CSV files.");
      return;
    }
    if (valid.length !== files.length) {
      alert("Some files were skipped because only images, PDFs, and CSV files are allowed.");
    }
    var dt = new DataTransfer();
    valid.forEach(function (f) { dt.items.add(f); });
    fileInput.files = dt.files;
    showFiles(valid);
  }

  function showFiles(files) {
    var names = files.map(function (f) { return f.name; });
    fileName.textContent = names.slice(0, 2).join(", ");
    if (files.length > 2) fileName.textContent += ", \u2026";
    if (files.length > 1) {
      fileCount.textContent = files.length + " files selected";
      fileCount.classList.remove("hidden");
    } else {
      fileCount.classList.add("hidden");
      fileCount.textContent = "";
    }
    filePreview.classList.remove("hidden");
    dropZone.classList.add("hidden");
    var mobileActions = document.querySelector(".mobile-upload-actions");
    if (mobileActions) mobileActions.classList.add("hidden");
    submitBtn.disabled = false;
  }

  function clearFile() {
    fileInput.value = "";
    if (cameraInput) cameraInput.value = "";
    filePreview.classList.add("hidden");
    dropZone.classList.remove("hidden");
    fileCount.classList.add("hidden");
    fileCount.textContent = "";
    var mobileActions = document.querySelector(".mobile-upload-actions");
    if (mobileActions) mobileActions.classList.remove("hidden");
    submitBtn.disabled = true;
  }

  dropZone.addEventListener("click", function (e) {
    if (e.target.tagName === "LABEL") return;
    fileInput.click();
  });

  fileInput.addEventListener("change", function () {
    if (fileInput.files.length) setFiles(fileInput.files);
  });

  if (mobileFileBtn) {
    mobileFileBtn.addEventListener("click", function () {
      if (cameraInput) { cameraInput.click(); } else { fileInput.click(); }
    });
  }

  if (cameraInput) {
    cameraInput.addEventListener("change", function () {
      if (!cameraInput.files.length) return;
      setFiles(cameraInput.files);
    });
  }

  clearBtn.addEventListener("click", clearFile);

  dropZone.addEventListener("dragover", function (e) {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });
  dropZone.addEventListener("dragleave", function () { dropZone.classList.remove("drag-over"); });
  dropZone.addEventListener("drop", function (e) {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    setFiles(e.dataTransfer.files);
  });

  /* ── Async submit + polling ── */

  function showProcessing(msg) {
    if (processingCard) {
      processingCard.classList.remove("hidden");
      if (processingMsg) processingMsg.textContent = msg || "Processing your material list\u2026";
      if (processingErr) { processingErr.textContent = ""; processingErr.classList.add("hidden"); }
    }
    // Hide the upload form
    form.classList.add("hidden");
  }

  function updateProgress(msg) {
    if (processingMsg && msg) processingMsg.textContent = msg;
  }

  function showError(msg) {
    if (processingCard) processingCard.classList.remove("hidden");
    if (processingErr) {
      processingErr.textContent = msg || "An error occurred.";
      processingErr.classList.remove("hidden");
    }
    if (processingMsg) processingMsg.textContent = "";
    // Show form again so user can retry
    form.classList.remove("hidden");
    submitBtn.disabled = false;
    spinner.classList.add("hidden");
  }

  function pollSession(statusUrl) {
    var interval = setInterval(function () {
      fetch(statusUrl)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          updateProgress(data.progress_message);

          if (data.status === "matched") {
            clearInterval(interval);
            updateProgress("Done! Redirecting to review\u2026");
            window.location.href = data.redirect;
          } else if (data.status === "error") {
            clearInterval(interval);
            showError(data.error || "Processing failed.");
          }
        })
        .catch(function () {
          /* network blip — keep polling */
        });
    }, 2000);
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    submitBtn.disabled = true;
    spinner.classList.remove("hidden");

    var formData = new FormData(form);

    fetch(form.action, { method: "POST", body: formData })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) {
          showError(data.error);
          return;
        }
        showProcessing("Starting\u2026");
        pollSession(data.status_url);
      })
      .catch(function (err) {
        showError("Upload failed: " + (err.message || "network error"));
      });
  });
}());
