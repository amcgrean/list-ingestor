/* Upload page interactions */
(function () {
  const dropZone   = document.getElementById("drop-zone");
  const fileInput  = document.getElementById("file-input");
  const filePreview = document.getElementById("file-preview");
  const fileName   = document.getElementById("file-name");
  const clearBtn   = document.getElementById("clear-file");
  const submitBtn  = document.getElementById("submit-btn");
  const spinner    = document.getElementById("spinner");
  const form       = document.getElementById("upload-form");

  function showFile(file) {
    fileName.textContent = file.name;
    filePreview.classList.remove("hidden");
    dropZone.classList.add("hidden");
    submitBtn.disabled = false;
  }

  function clearFile() {
    fileInput.value = "";
    filePreview.classList.add("hidden");
    dropZone.classList.remove("hidden");
    submitBtn.disabled = true;
  }

  // Click on drop zone opens file picker
  dropZone.addEventListener("click", () => fileInput.click());

  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) showFile(fileInput.files[0]);
  });

  clearBtn.addEventListener("click", clearFile);

  // Drag and drop
  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const ext = file.name.split(".").pop().toLowerCase();
    if (!["jpg", "jpeg", "png", "pdf"].includes(ext)) {
      alert("Unsupported file type. Please upload JPG, PNG, or PDF.");
      return;
    }
    // Assign to input via DataTransfer
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    showFile(file);
  });

  // Show spinner on submit
  form.addEventListener("submit", () => {
    submitBtn.disabled = true;
    spinner.classList.remove("hidden");
    submitBtn.querySelector(".spinner").style.display = "inline-block";
  });
}());
