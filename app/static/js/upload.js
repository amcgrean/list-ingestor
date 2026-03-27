/* Upload page interactions — supports multi-file selection */
(function () {
  const dropZone    = document.getElementById("drop-zone");
  const fileInput   = document.getElementById("file-input");
  const cameraInput = document.getElementById("camera-input");
  const cameraBtn   = document.getElementById("camera-btn");
  const galleryBtn  = document.getElementById("gallery-btn");
  const filePreview = document.getElementById("file-preview");
  const fileName    = document.getElementById("file-name");
  const clearBtn    = document.getElementById("clear-file");
  const submitBtn   = document.getElementById("submit-btn");
  const spinner     = document.getElementById("spinner");
  const form        = document.getElementById("upload-form");

  function showFiles(files) {
    if (files.length === 1) {
      fileName.textContent = files[0].name;
    } else {
      fileName.textContent = files[0].name + " (+" + (files.length - 1) + " more)";
    }
    filePreview.classList.remove("hidden");
    dropZone.classList.add("hidden");
    document.querySelector(".mobile-upload-actions").classList.add("hidden");
    submitBtn.disabled = false;
  }

  function clearFile() {
    fileInput.value = "";
    if (cameraInput) cameraInput.value = "";
    filePreview.classList.add("hidden");
    dropZone.classList.remove("hidden");
    document.querySelector(".mobile-upload-actions").classList.remove("hidden");
    submitBtn.disabled = true;
  }

  // Click on drop zone opens file picker (desktop)
  dropZone.addEventListener("click", (e) => {
    if (e.target.tagName === "LABEL") return;
    fileInput.click();
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) showFiles(fileInput.files);
  });

  // Camera capture button (mobile)
  if (cameraBtn) {
    cameraBtn.addEventListener("click", () => cameraInput.click());
  }

  // Gallery / file browse button (mobile)
  if (galleryBtn) {
    galleryBtn.addEventListener("click", () => fileInput.click());
  }

  // Camera input: copy selected file into the main named input for form submission
  if (cameraInput) {
    cameraInput.addEventListener("change", () => {
      if (!cameraInput.files.length) return;
      const file = cameraInput.files[0];
      try {
        const dt = new DataTransfer();
        dt.items.add(file);
        fileInput.files = dt.files;
      } catch (_) {
        cameraInput.name = "file";
        fileInput.removeAttribute("name");
      }
      showFiles([file]);
    });
  }

  clearBtn.addEventListener("click", clearFile);

  // Drag and drop (desktop) — supports multiple files
  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    const files = e.dataTransfer.files;
    if (!files.length) return;
    const allowed = ["jpg", "jpeg", "png", "pdf", "heic", "heif", "webp"];
    for (let i = 0; i < files.length; i++) {
      const ext = files[i].name.split(".").pop().toLowerCase();
      if (!allowed.includes(ext) && !files[i].type.startsWith("image/")) {
        alert("Unsupported file type: " + files[i].name);
        return;
      }
    }
    const dt = new DataTransfer();
    for (let i = 0; i < files.length; i++) {
      dt.items.add(files[i]);
    }
    fileInput.files = dt.files;
    showFiles(files);
  });

  // Show spinner on submit
  form.addEventListener("submit", () => {
    submitBtn.disabled = true;
    spinner.classList.remove("hidden");
    submitBtn.querySelector(".spinner").style.display = "inline-block";
  });
}());
