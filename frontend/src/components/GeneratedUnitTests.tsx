import React, { useState } from 'react';
import { TestTube2, Copy, Check, Terminal } from 'lucide-react';

interface GeneratedUnitTestsProps {
  unitTests?: string | null;
}

export const GeneratedUnitTests: React.FC<GeneratedUnitTestsProps> = ({ unitTests }) => {
  const [copied, setCopied] = useState(false);

  const testContent = unitTests && unitTests.trim()
    ? unitTests.trim()
    : '# No dynamic pytest suite generated for this changeset.';

  const handleCopy = () => {
    navigator.clipboard.writeText(testContent);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="bg-[#12151c] border border-slate-800/80 rounded-xl p-4 shadow-md flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between pb-3 border-b border-slate-800/80">
        <div className="flex items-center space-x-2.5">
          <div className="w-7 h-7 rounded-lg bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center text-emerald-400">
            <TestTube2 className="w-3.5 h-3.5" />
          </div>
          <div>
            <h3 className="text-xs font-bold text-white tracking-tight uppercase">Generated Pytest Suite</h3>
            <p className="text-[10px] text-slate-400">AST parameter mocks & regression tests</p>
          </div>
        </div>

        {/* Copy Button */}
        {unitTests && unitTests.trim() && (
          <button
            onClick={handleCopy}
            className="flex items-center gap-1 px-2 py-1 rounded bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-300 border border-emerald-500/20 text-[10px] font-semibold transition-all cursor-pointer"
            title="Copy test suite code"
          >
            {copied ? (
              <>
                <Check className="w-3 h-3 text-emerald-400" />
                <span>Copied</span>
              </>
            ) : (
              <>
                <Copy className="w-3 h-3" />
                <span>Copy Tests</span>
              </>
            )}
          </button>
        )}
      </div>

      {/* Code Viewer */}
      <div className="mt-3 flex-1 flex flex-col">
        <div className="rounded-lg bg-[#0a0c10] border border-slate-800/80 overflow-hidden">
          <div className="px-3 py-1.5 bg-slate-900/90 border-b border-slate-800 flex items-center justify-between text-[10px] text-slate-400 font-mono">
            <span className="flex items-center gap-1 text-slate-300">
              <Terminal className="w-3 h-3 text-emerald-400" />
              test_suite_generated.py
            </span>
            <span>pytest</span>
          </div>

          <pre className="p-3 text-[11px] font-mono text-emerald-300 overflow-x-auto leading-relaxed max-h-[300px]">
            <code>{testContent}</code>
          </pre>
        </div>
      </div>
    </div>
  );
};
