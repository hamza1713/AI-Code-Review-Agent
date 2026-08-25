import React, { useState, useCallback } from 'react';
import { useDropzone } from 'react-dropzone';
import { FileCode, Upload, FolderArchive, Sparkles, Trash2, ArrowRight, AlertTriangle, CheckCircle2, ShieldAlert } from 'lucide-react';

interface ReviewInputProps {
  onSubmit: (payload: { rawDiff?: string; file?: File; zipFile?: File }) => void;
  isLoading: boolean;
}

const SAMPLE_SQLI_DIFF = `diff --git a/app/user_auth.py b/app/user_auth.py
--- a/app/user_auth.py
+++ b/app/user_auth.py
@@ -12,6 +12,16 @@
+def authenticate_user(username, password):
+    # Query user database directly without parameterized query
+    query = f"SELECT * FROM users WHERE username = '{username}'"
+    user = db.query(query)
+    if user and user.password == password:
+        print(f"User {username} authenticated successfully")
+        return True
+    return False
`;

const SAMPLE_CLEAN_DIFF = `diff --git a/app/math_utils.py b/app/math_utils.py
--- a/app/math_utils.py
+++ b/app/math_utils.py
@@ -5,4 +5,8 @@
-def add(a, b):
-    return a + b
+def calculate_total(a: float, b: float) -> float:
+    """Compute sum with strict typing and boundary validation."""
+    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
+        raise TypeError("Inputs must be numeric types")
+    return float(a + b)
`;

const SAMPLE_GOV_VIOLATION_DIFF = `diff --git a/services/payment.py b/services/payment.py
--- a/services/payment.py
+++ b/services/payment.py
@@ -20,6 +20,10 @@
+def process_transaction(card_token, amount):
+    # Violates governance rule: no raw print statements in production
+    print(f"Processing transaction for token: {card_token}")
+    # Violates governance rule: missing docstring on public method
+    return gateway.charge(token=card_token, amount=amount)
`;

export const ReviewInput: React.FC<ReviewInputProps> = ({ onSubmit, isLoading }) => {
  const [activeTab, setActiveTab] = useState<'diff' | 'file' | 'zip'>('diff');
  const [diffContent, setDiffContent] = useState('');
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [selectedZip, setSelectedZip] = useState<File | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Dropzone for Single File
  const onDropFile = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0) {
      const file = acceptedFiles[0];
      if (file.size > 2 * 1024 * 1024) {
        setErrorMessage('File exceeds the 2MB maximum limit.');
        return;
      }
      setSelectedFile(file);
      setErrorMessage(null);
    }
  }, []);

  const {
    getRootProps: getFileRootProps,
    getInputProps: getFileInputProps,
    isDragActive: isFileDragActive
  } = useDropzone({
    onDrop: onDropFile,
    multiple: false,
    maxSize: 2 * 1024 * 1024
  });

  // Dropzone for ZIP Archive
  const onDropZip = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0) {
      const file = acceptedFiles[0];
      if (!file.name.toLowerCase().endsWith('.zip')) {
        setErrorMessage('Only .zip archive files are accepted.');
        return;
      }
      if (file.size > 2 * 1024 * 1024) {
        setErrorMessage('ZIP archive exceeds the 2MB maximum limit.');
        return;
      }
      setSelectedZip(file);
      setErrorMessage(null);
    }
  }, []);

  const {
    getRootProps: getZipRootProps,
    getInputProps: getZipInputProps,
    isDragActive: isZipDragActive
  } = useDropzone({
    onDrop: onDropZip,
    multiple: false,
    accept: { 'application/zip': ['.zip'] },
    maxSize: 2 * 1024 * 1024
  });

  const handleClear = () => {
    setDiffContent('');
    setSelectedFile(null);
    setSelectedZip(null);
    setErrorMessage(null);
  };

  const handleStartReview = () => {
    setErrorMessage(null);
    if (activeTab === 'diff') {
      if (!diffContent.trim()) {
        setErrorMessage('Please paste a unified git diff before submitting.');
        return;
      }
      onSubmit({ rawDiff: diffContent });
    } else if (activeTab === 'file') {
      if (!selectedFile) {
        setErrorMessage('Please select or drop a source code file to review.');
        return;
      }
      onSubmit({ file: selectedFile });
    } else if (activeTab === 'zip') {
      if (!selectedZip) {
        setErrorMessage('Please select or drop a repository .zip archive.');
        return;
      }
      onSubmit({ zipFile: selectedZip });
    }
  };

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-4 sm:p-5 shadow-lg relative overflow-hidden">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between pb-4 border-b border-slate-800/80 gap-3 relative z-10">
        <div>
          <h2 className="text-base sm:text-lg font-bold text-white tracking-tight flex items-center gap-2">
            <span>Submit Code for Review</span>
          </h2>
          <p className="text-[11px] text-slate-400 mt-0.5">
            Analyze code against security pattern heuristics, AST cross-file impact, and team governance policies.
          </p>
        </div>

        {/* Tab Selection */}
        <div className="flex bg-slate-900/90 p-1 rounded-lg border border-slate-800 self-start sm:self-auto">
          <button
            type="button"
            onClick={() => { setActiveTab('diff'); setErrorMessage(null); }}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'diff'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <FileCode className="w-3.5 h-3.5" />
            <span>Paste Diff</span>
          </button>
          <button
            type="button"
            onClick={() => { setActiveTab('file'); setErrorMessage(null); }}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'file'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <Upload className="w-3.5 h-3.5" />
            <span>Upload File</span>
          </button>
          <button
            type="button"
            onClick={() => { setActiveTab('zip'); setErrorMessage(null); }}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-md text-xs font-semibold transition-all cursor-pointer ${
              activeTab === 'zip'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <FolderArchive className="w-3.5 h-3.5" />
            <span>Upload ZIP</span>
          </button>
        </div>
      </div>

      {/* Content Area */}
      <div className="mt-4 relative z-10">
        {/* Tab 1: Paste Diff */}
        {activeTab === 'diff' && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-xs font-medium text-slate-300">Unified Git Diff Content</span>
              {/* Presets */}
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[10px] text-slate-500 font-medium">Quick Presets:</span>
                <button
                  type="button"
                  onClick={() => { setDiffContent(SAMPLE_SQLI_DIFF); setErrorMessage(null); }}
                  className="text-[11px] px-2 py-0.5 rounded bg-rose-500/10 text-rose-300 border border-rose-500/20 hover:bg-rose-500/20 transition-colors flex items-center gap-1 cursor-pointer"
                >
                  <ShieldAlert className="w-3 h-3" />
                  <span>SQLi & Auth PR</span>
                </button>
                <button
                  type="button"
                  onClick={() => { setDiffContent(SAMPLE_CLEAN_DIFF); setErrorMessage(null); }}
                  className="text-[11px] px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-300 border border-emerald-500/20 hover:bg-emerald-500/20 transition-colors flex items-center gap-1 cursor-pointer"
                >
                  <CheckCircle2 className="w-3 h-3" />
                  <span>Clean Refactor</span>
                </button>
                <button
                  type="button"
                  onClick={() => { setDiffContent(SAMPLE_GOV_VIOLATION_DIFF); setErrorMessage(null); }}
                  className="text-[11px] px-2 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/20 hover:bg-amber-500/20 transition-colors flex items-center gap-1 cursor-pointer"
                >
                  <AlertTriangle className="w-3 h-3" />
                  <span>Governance PR</span>
                </button>
              </div>
            </div>

            <div className="relative rounded-xl border border-slate-800 bg-[#0a0c10] overflow-hidden focus-within:border-indigo-500/60 focus-within:ring-1 focus-within:ring-indigo-500/30 transition-all">
              <textarea
                value={diffContent}
                onChange={(e) => setDiffContent(e.target.value)}
                placeholder={`diff --git a/app/user_auth.py b/app/user_auth.py\n--- a/app/user_auth.py\n+++ b/app/user_auth.py\n@@ -10,6 +10,12 @@\n+def authenticate_user(username, password):\n+    query = f"SELECT * FROM users WHERE username = '{username}'"\n+    return db.query(query)`}
                rows={10}
                className="w-full bg-transparent p-3.5 text-xs font-mono text-slate-200 placeholder-slate-600 focus:outline-none resize-y leading-relaxed"
                spellCheck={false}
              />
              <div className="px-3.5 py-1.5 bg-slate-900/60 border-t border-slate-800/80 flex items-center justify-between text-[10px] text-slate-400 font-mono">
                <span>{diffContent ? `${diffContent.split('\n').length} lines • ${new Blob([diffContent]).size} bytes` : '0 lines'}</span>
                <span>Supports standard unified git diff format</span>
              </div>
            </div>
          </div>
        )}

        {/* Tab 2: Upload File */}
        {activeTab === 'file' && (
          <div className="space-y-3">
            <div
              {...getFileRootProps()}
              className={`border-2 border-dashed rounded-xl p-6 sm:p-8 text-center transition-all cursor-pointer ${
                isFileDragActive
                  ? 'border-indigo-500 bg-indigo-500/10'
                  : selectedFile
                  ? 'border-emerald-500/50 bg-emerald-500/5'
                  : 'border-slate-800 hover:border-slate-700 bg-slate-900/40'
              }`}
            >
              <input {...getFileInputProps()} />
              <div className="w-10 h-10 mx-auto mb-2 rounded-xl bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
                <Upload className="w-5 h-5" />
              </div>
              <h3 className="text-xs font-semibold text-white">
                {selectedFile ? selectedFile.name : 'Drag & drop a single source code file here'}
              </h3>
              <p className="text-[11px] text-slate-400 mt-0.5">
                {selectedFile
                  ? `Size: ${(selectedFile.size / 1024).toFixed(1)} KB • Click or drop again to replace`
                  : 'Python (.py), JavaScript (.js, .ts), Go (.go), SQL (.sql), Rust (.rs), etc. (Max 2MB)'}
              </p>
              {selectedFile && (
                <div className="mt-3 inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[11px] font-mono font-medium">
                  <CheckCircle2 className="w-3 h-3" />
                  <span>{selectedFile.name} ready for review</span>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Tab 3: Upload ZIP */}
        {activeTab === 'zip' && (
          <div className="space-y-3">
            <div
              {...getZipRootProps()}
              className={`border-2 border-dashed rounded-xl p-6 sm:p-8 text-center transition-all cursor-pointer ${
                isZipDragActive
                  ? 'border-indigo-500 bg-indigo-500/10'
                  : selectedZip
                  ? 'border-emerald-500/50 bg-emerald-500/5'
                  : 'border-slate-800 hover:border-slate-700 bg-slate-900/40'
              }`}
            >
              <input {...getZipInputProps()} />
              <div className="w-10 h-10 mx-auto mb-2 rounded-xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center text-purple-400">
                <FolderArchive className="w-5 h-5" />
              </div>
              <h3 className="text-xs font-semibold text-white">
                {selectedZip ? selectedZip.name : 'Drag & drop a repository ZIP archive here'}
              </h3>
              <p className="text-[11px] text-slate-400 mt-0.5">
                {selectedZip
                  ? `Size: ${(selectedZip.size / 1024).toFixed(1)} KB • Click or drop again to replace`
                  : 'Small project archive (max 20 files, max 2MB). AST call graph will index callers across repository.'}
              </p>
              {selectedZip && (
                <div className="mt-3 inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-[11px] font-mono font-medium">
                  <CheckCircle2 className="w-3 h-3" />
                  <span>{selectedZip.name} ready for repository review</span>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Error Alert */}
        {errorMessage && (
          <div className="mt-3 p-3 rounded-lg bg-rose-500/10 border border-rose-500/20 text-rose-300 text-xs flex items-center gap-2.5">
            <AlertTriangle className="w-4 h-4 shrink-0 text-rose-400" />
            <span>{errorMessage}</span>
          </div>
        )}

        {/* Action Bar */}
        <div className="mt-4 flex items-center justify-between pt-3 border-t border-slate-800/80">
          <button
            type="button"
            onClick={handleClear}
            className="flex items-center gap-1 text-xs text-slate-400 hover:text-slate-200 transition-colors px-2.5 py-1.5 rounded-lg hover:bg-slate-800/50 cursor-pointer"
          >
            <Trash2 className="w-3 h-3" />
            <span>Clear</span>
          </button>

          <button
            type="button"
            onClick={handleStartReview}
            disabled={isLoading}
            className="flex items-center gap-2 px-5 py-2 rounded-xl bg-gradient-to-r from-indigo-600 via-indigo-500 to-purple-600 text-white text-xs font-bold shadow-md shadow-indigo-500/20 hover:shadow-indigo-500/40 hover:scale-[1.01] active:scale-[0.99] transition-all disabled:opacity-50 disabled:pointer-events-none cursor-pointer"
          >
            <Sparkles className="w-3.5 h-3.5" />
            <span>Run Multi-Agent Review</span>
            <ArrowRight className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
};
