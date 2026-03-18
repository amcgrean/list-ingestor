/* Upload page interactions */
(function () {
  const dropZone    = document.getElementById("drop-zone");
  const fileInput   = document.getElementById("file-input");
  const cameraInput = document.getElementById("camera-input");
  const mobileFileBtn = document.getElementById("mobile-file-btn");
  const filePreview = document.getElementById("file-preview");
  const fileName    = document.getElementById("file-name");
  const fileCount   = document.getElementById("file-count");
  const clearBtn    = document.getElementById("clear-file");
  const submitBtn   = document.getElementById("submit-btn");
  const spinner     = document.getElementById("spinner");
  const form        = document.getElementById("upload-form");

  function isAllowedFile(file) {
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    const allowedExts = ["jpg", "jpeg", "png", "pdf", "webp", "csv"];
    if (allowedExts.includes(ext)) return true;
    return file.type.startsWith("image/") || file.type === "application/pdf" || file.type === "text/csv";
  }

  function setFiles(files) {
    if (!files || !files.length) return;
    const valid = Array.from(files).filter(isAllowedFile);
    if (!valid.length) {
      alert("Unsupported file type. Please upload images, PDFs, or CSV files.");
      return;
    }
    if (valid.length !== files.length) {
      alert("Some files were skipped because only images, PDFs, and CSV files are allowed.");
    }

    const dt = new DataTransfer();
    valid.forEach((f) => dt.items.add(f));
    fileInput.files = dt.files;
    showFiles(valid);
  }

  function showFiles(files) {
    const names = files.map((f) => f.name);
    fileName.textContent = names.slice(0, 2).join(", ");
    if (files.length > 2) {
      fileName.textContent += ", …";
    }
    if (files.length > 1) {
      fileCount.textContent = `${files.length} files selected`;
      fileCount.classList.remove("hidden");
    } else {
      fileCount.classList.add("hidden");
      fileCount.textContent = "";
    }
    filePreview.classList.remove("hidden");
    dropZone.classList.add("hidden");
    const mobileActions = document.querySelector(".mobile-upload-actions");
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
    const mobileActions = document.querySelector(".mobile-upload-actions");
    if (mobileActions) mobileActions.classList.remove("hidden");
    submitBtn.disabled = true;
  }

  dropZone.addEventListener("click", (e) => {
    if (e.target.tagName === "LABEL") return;
    fileInput.click();
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) setFiles(fileInput.files);
  });

  if (mobileFileBtn) {
    mobileFileBtn.addEventListener("click", () => {
      if (cameraInput) {
        cameraInput.click();
      } else {
        fileInput.click();
      }
    });
  }

  if (cameraInput) {
    cameraInput.addEventListener("change", () => {
      if (!cameraInput.files.length) return;
      setFiles(cameraInput.files);
    });
  }

  clearBtn.addEventListener("click", clearFile);

  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    setFiles(e.dataTransfer.files);
  });

  form.addEventListener("submit", () => {
    submitBtn.disabled = true;
    spinner.classList.remove("hidden");
    submitBtn.querySelector(".spinner").style.display = "inline-block";
  });
}());
