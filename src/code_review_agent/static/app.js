// AI Code Review Agent — Single-Page Web UI Client

function initCodeReviewApp() {
  // DOM Elements
  const tabBtns = document.querySelectorAll(".tab-btn");
  const tabPanes = document.querySelectorAll(".tab-pane");
  const diffText = document.getElementById("diffText");
  const loadSampleBtn = document.getElementById("loadSampleBtn");
  const startReviewBtn = document.getElementById("startReviewBtn");
  const clearBtn = document.getElementById("clearBtn");
  const errorAlert = document.getElementById("errorAlert");
  const errorText = document.getElementById("errorText");

  // File Upload Elements
  const fileDropzone = document.getElementById("fileDropzone");
  const fileInput = document.getElementById("fileInput");
  const browseFileBtn = document.getElementById("browseFileBtn");
  const filePreview = document.getElementById("filePreview");

  // Zip Upload Elements
  const zipDropzone = document.getElementById("zipDropzone");
  const zipInput = document.getElementById("zipInput");
  const browseZipBtn = document.getElementById("browseZipBtn");
  const zipPreview = document.getElementById("zipPreview");

  // Sections
  const inputSection = document.getElementById("inputSection");
  const progressSection = document.getElementById("progressSection");
  const resultsSection = document.getElementById("resultsSection");
  const reviewAnotherBtn = document.getElementById("reviewAnotherBtn");
  const copyTestsBtn = document.getElementById("copyTestsBtn");

  let activeTab = "diffTab";
  let selectedFile = null;
  let selectedZip = null;
  let stageInterval = null;

  // Sample SQLi Diff
  const SAMPLE_SQLI_DIFF = `diff --git a/app/user_auth.py b/app/user_auth.py
--- a/app/user_auth.py
+++ b/app/user_auth.py
@@ -12,6 +12,16 @@
+def authenticate_user(username, password):
+    # Query user database directly
+    query = f"SELECT * FROM users WHERE username = '{username}'"
+    user = db.query(query)
+    if user and user.password == password:
+        print(f"User {username} authenticated successfully")
+        return True
+    return False
`;

  // Tab Switching
  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      tabBtns.forEach(b => {
        b.classList.remove("active");
        b.setAttribute("aria-selected", "false");
      });
      tabPanes.forEach(p => p.classList.remove("active"));

      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");
      activeTab = btn.dataset.tab;
      document.getElementById(activeTab).classList.add("active");
      hideError();
    });
  });

  // Load Sample Diff
  loadSampleBtn.addEventListener("click", () => {
    diffText.value = SAMPLE_SQLI_DIFF;
    hideError();
  });

  // Clear Inputs
  clearBtn.addEventListener("click", () => {
    diffText.value = "";
    selectedFile = null;
    selectedZip = null;
    fileInput.value = "";
    zipInput.value = "";
    filePreview.style.display = "none";
    zipPreview.style.display = "none";
    fileDropzone.classList.remove("has-file");
    zipDropzone.classList.remove("has-file");
    hideError();
  });

  // File Upload Handlers
  browseFileBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    fileInput.click();
  });
  fileInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files[0]) {
      handleFileSelected(e.target.files[0]);
    }
  });

  setupDropzone(fileDropzone, fileInput, (file) => {
    handleFileSelected(file);
  });

  function handleFileSelected(file) {
    selectedFile = file;
    filePreview.style.display = "inline-flex";
    filePreview.innerHTML = `✅ 📄 <strong>${escapeHtml(file.name)}</strong> (${formatBytes(file.size)})`;
    fileDropzone.classList.add("has-file");
    hideError();
  }

  // Zip Upload Handlers
  browseZipBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    zipInput.click();
  });
  zipInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files[0]) {
      handleZipSelected(e.target.files[0]);
    }
  });

  setupDropzone(zipDropzone, zipInput, (file) => {
    if (file.name.toLowerCase().endsWith(".zip")) {
      handleZipSelected(file);
    } else {
      showError("Please upload a valid .zip archive file.");
    }
  });

  function handleZipSelected(file) {
    if (!file.name.toLowerCase().endsWith(".zip")) {
      showError("Please upload a valid .zip archive file.");
      return;
    }
    selectedZip = file;
    zipPreview.style.display = "inline-flex";
    zipPreview.innerHTML = `✅ 📦 <strong>${escapeHtml(file.name)}</strong> (${formatBytes(file.size)})`;
    zipDropzone.classList.add("has-file");
    hideError();
  }

  function setupDropzone(zone, inputElement, onFile) {
    zone.addEventListener("click", (e) => {
      // Trigger file browser when clicking anywhere in dropzone except on input itself
      if (e.target !== inputElement) {
        inputElement.click();
      }
    });

    zone.addEventListener("dragover", (e) => {
      e.preventDefault();
      zone.classList.add("drag-over");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("drag-over");
      if (e.dataTransfer.files && e.dataTransfer.files[0]) {
        onFile(e.dataTransfer.files[0]);
      }
    });
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  }

  function showError(msg) {
    errorText.innerText = msg;
    errorAlert.style.display = "flex";
  }

  function hideError() {
    errorAlert.style.display = "none";
  }

  // Review Pipeline Execution
  startReviewBtn.addEventListener("click", async () => {
    hideError();

    let formData = new FormData();
    let isJson = false;
    let jsonPayload = {};

    if (activeTab === "diffTab") {
      const diffContent = diffText.value.trim();
      if (!diffContent) {
        showError("Please paste a unified git diff before running the review.");
        return;
      }
      isJson = true;
      jsonPayload = { raw_diff: diffContent };
    } else if (activeTab === "fileTab") {
      if (!selectedFile) {
        showError("Please select or drop a source code file to review.");
        return;
      }
      formData.append("file", selectedFile);
    } else if (activeTab === "zipTab") {
      if (!selectedZip) {
        showError("Please select or drop a .zip archive to review.");
        return;
      }
      formData.append("zip_file", selectedZip);
    }

    // Enter Progress State
    showProgressState();

    try {
      let response;
      if (isJson) {
        response = await fetch("/api/review", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(jsonPayload)
        });
      } else {
        response = await fetch("/api/review", {
          method: "POST",
          body: formData
        });
      }

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        const errMsg = errData.detail || `Server returned HTTP ${response.status}: ${response.statusText}`;
        throw new Error(errMsg);
      }

      const reviewData = await response.json();
      renderResults(reviewData);

    } catch (err) {
      stopStageAnimation();
      progressSection.style.display = "none";
      inputSection.style.display = "block";
      showError(err.message || "An unexpected error occurred during review.");
    }
  });

  // Stage Progress Animation
  function showProgressState() {
    inputSection.style.display = "none";
    resultsSection.style.display = "none";
    progressSection.style.display = "block";

    const stages = [
      document.getElementById("stage1"),
      document.getElementById("stage2"),
      document.getElementById("stage3"),
      document.getElementById("stage4"),
      document.getElementById("stage5")
    ];

    stages.forEach((s, idx) => {
      s.classList.remove("active", "completed");
      if (idx === 0) s.classList.add("active");
    });

    let current = 0;
    stageInterval = setInterval(() => {
      if (current < stages.length - 1) {
        stages[current].classList.remove("active");
        stages[current].classList.add("completed");
        current++;
        stages[current].classList.add("active");
      }
    }, 1200);
  }

  function stopStageAnimation() {
    if (stageInterval) {
      clearInterval(stageInterval);
      stageInterval = null;
    }
  }

  // Render Review Results
  function renderResults(data) {
    stopStageAnimation();
    progressSection.style.display = "none";
    resultsSection.style.display = "flex";

    // 1. Verdict Banner
    const verdictBanner = document.getElementById("verdictBanner");
    const verdictTag = document.getElementById("verdictTag");
    const verdictTitle = document.getElementById("verdictTitle");
    const verdictSummary = document.getElementById("verdictSummary");
    const confidenceValue = document.getElementById("confidenceValue");

    verdictBanner.className = "verdict-banner";
    const verdict = (data.verdict || "APPROVE").toUpperCase();
    verdictTag.innerText = verdict;
    confidenceValue.innerText = `${data.confidence_score || 0}/100`;
    verdictSummary.innerText = data.summary || "Review completed.";

    if (verdict.includes("ESCALATE")) {
      verdictBanner.classList.add("verdict-escalate");
      verdictTitle.innerText = "ESCALATE 🚨 Critical Risk Detected";
    } else if (verdict.includes("REQUEST") || verdict.includes("CHANGE")) {
      verdictBanner.classList.add("verdict-request-changes");
      verdictTitle.innerText = "REQUEST CHANGES ⚠️ Action Required";
    } else {
      verdictBanner.classList.add("verdict-approve");
      verdictTitle.innerText = "APPROVE ✅ Merge Safe";
    }

    // 2. Heuristic Pattern Scanner Findings
    const patternList = document.getElementById("patternFindingsList");
    const patternCount = document.getElementById("patternFindingsCount");
    const findings = data.pattern_findings || [];
    patternCount.innerText = findings.length;

    if (findings.length === 0) {
      patternList.innerHTML = `<p class="empty-msg">✅ No common security anti-patterns detected (${data.pattern_findings_label || 'heuristic scanner'}).</p>`;
    } else {
      patternList.innerHTML = findings.map(f => `
        <div class="finding-card">
          <div class="finding-header">
            <span class="finding-loc">${escapeHtml(f.file_path)}:L${f.line_number}</span>
            <span class="finding-sev sev-${(f.severity || 'medium').toLowerCase()}">${escapeHtml(f.severity)} • ${escapeHtml(f.rule_id || '')}</span>
          </div>
          <div class="finding-desc"><strong>${escapeHtml(f.cwe ? f.cwe + ': ' : '')}</strong>${escapeHtml(f.description)}</div>
          ${f.snippet ? `<pre class="finding-code"><code>${escapeHtml(f.snippet)}</code></pre>` : ''}
          ${f.fix_recommendation ? `<div class="finding-fix">💡 Suggested Fix: ${escapeHtml(f.fix_recommendation)}</div>` : ''}
        </div>
      `).join("");
    }

    // 3. Governance Violations
    const govList = document.getElementById("governanceList");
    const govCount = document.getElementById("governanceCount");
    const violations = data.governance_violations || [];
    govCount.innerText = violations.length;

    if (violations.length === 0) {
      govList.innerHTML = `<p class="empty-msg">✅ All team governance rules and coding standards passed (.code-review.yaml).</p>`;
    } else {
      govList.innerHTML = violations.map(v => `
        <div class="finding-card">
          <div class="finding-header">
            <span class="finding-loc">${escapeHtml(v.file_path)}:L${v.line_number}</span>
            <span class="finding-sev sev-${(v.severity || 'warning').toLowerCase()}">${escapeHtml(v.severity)} • ${escapeHtml(v.rule_id)}</span>
          </div>
          <div class="finding-desc"><strong>${escapeHtml(v.rule_name)}:</strong> ${escapeHtml(v.description)}</div>
          ${v.suggested_fix ? `<div class="finding-fix">🛠️ Action: ${escapeHtml(v.suggested_fix)}</div>` : ''}
        </div>
      `).join("");
    }

    // 4. Inline Comments
    const inlineCard = document.getElementById("inlineCommentsCard");
    const inlineList = document.getElementById("inlineCommentsList");
    const inlineCount = document.getElementById("inlineCommentsCount");
    const inlineComments = data.inline_comments || [];
    inlineCount.innerText = inlineComments.length;

    if (inlineComments.length === 0) {
      inlineList.innerHTML = `<p class="empty-msg">No line-level inline comments generated.</p>`;
    } else {
      inlineList.innerHTML = inlineComments.map(c => `
        <div class="finding-card">
          <div class="finding-header">
            <span class="finding-loc">${escapeHtml(c.path)}:L${c.line}</span>
            <span class="finding-sev sev-${(c.severity || 'info').toLowerCase()}">${escapeHtml(c.severity)}</span>
          </div>
          <div class="finding-desc">${escapeHtml(c.comment_body)}</div>
          ${c.suggestion_code ? `<pre class="finding-code"><code>```suggestion\n${escapeHtml(c.suggestion_code)}\n\`\`\`</code></pre>` : ''}
        </div>
      `).join("");
    }

    // 5. Cross-File Impact Panel
    const impactBadge = document.getElementById("impactStatusBadge");
    const impactContent = document.getElementById("impactContent");
    const impact = data.cross_file_impact || {};

    if (!impact.available || !impact.is_python) {
      impactBadge.className = "badge badge-outline";
      impactBadge.innerText = "Non-Python";
      impactContent.innerText = impact.message || "cross-file impact analysis unavailable: Python only";
    } else {
      impactBadge.className = "badge badge-info";
      impactBadge.innerText = "Python AST";
      impactContent.innerText = impact.message || "No cross-file caller dependencies detected in repository index.";
    }

    // 6. Generated Pytest Suite
    const unitTestsContent = document.getElementById("unitTestsContent");
    if (data.generated_unit_tests && data.generated_unit_tests.trim()) {
      unitTestsContent.innerText = data.generated_unit_tests.trim();
    } else {
      unitTestsContent.innerText = "# No dynamic test suite generated for this change.";
    }

    // 7. Telemetry & Cost Footer
    const t = data.telemetry || {};
    document.getElementById("tLatency").innerText = (t.duration_seconds || 0).toFixed(2) + "s";
    document.getElementById("tTokens").innerText = (t.total_tokens || 0).toLocaleString();
    document.getElementById("tBreakdown").innerText = `${(t.prompt_tokens || 0).toLocaleString()} / ${(t.completion_tokens || 0).toLocaleString()}`;
    document.getElementById("tCost").innerText = `$${(t.estimated_cost_usd || 0).toFixed(6)} USD`;
    document.getElementById("tModel").innerText = t.model_used || "gemini-3.1-flash-lite-preview";

    // 8. Scope Note
    if (data.scope_note) {
      document.getElementById("scopeNoteText").innerText = data.scope_note;
    }
  }

  // Copy Tests Button
  copyTestsBtn.addEventListener("click", () => {
    const code = document.getElementById("unitTestsContent").innerText;
    navigator.clipboard.writeText(code).then(() => {
      const orig = copyTestsBtn.innerText;
      copyTestsBtn.innerText = "Copied! ✅";
      setTimeout(() => copyTestsBtn.innerText = orig, 2000);
    });
  });

  // Review Another Button
  reviewAnotherBtn.addEventListener("click", () => {
    resultsSection.style.display = "none";
    inputSection.style.display = "block";
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
}

// Support both early script execution and deferred execution
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initCodeReviewApp);
} else {
  initCodeReviewApp();
}
