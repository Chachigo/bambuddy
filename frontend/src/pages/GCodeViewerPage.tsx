import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ArrowLeft, ExternalLink, ShieldAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { findFramingRefusal, type FrameProblem } from '../utils/framing';

export function GCodeViewerPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { t } = useTranslation();
  const [problem, setProblem] = useState<FrameProblem | null>(null);

  // Forward the outer page's query string (e.g. ?archive=82) to the iframe so
  // the adapter inside can pick up the archive to load. The iframe itself must
  // keep the trailing slash on /gcode-viewer/ so it hits the raw-viewer route;
  // the outer SPA URL uses no trailing slash so a reload falls through to the
  // SPA catch-all and keeps the Bambuddy layout shell.
  const iframeSrc = `/gcode-viewer/${window.location.search}`;
  const embedded = window !== window.top;

  // A frame refused by X-Frame-Options / frame-ancestors still fires `onLoad` —
  // the browser commits its own "refused to connect" error page — so the iframe
  // itself cannot tell us anything. Ask for the same URL directly instead: it is
  // same-origin, so every response header is readable, and it travels through
  // whatever proxy the browser reaches Bambuddy by. The iframe is rendered
  // straight away regardless and only replaced if this comes back refusing,
  // which keeps the working case exactly as fast as before.
  useEffect(() => {
    if (embedded) return;
    const controller = new AbortController();
    (async () => {
      try {
        const response = await fetch(iframeSrc, {
          credentials: 'same-origin',
          signal: controller.signal,
        });
        if (!response.ok) {
          setProblem({ kind: 'unavailable', detail: `HTTP ${response.status}` });
          return;
        }
        const refusal = findFramingRefusal(
          response.headers.get('x-frame-options'),
          response.headers.get('content-security-policy'),
          window.location.origin,
        );
        if (refusal) setProblem({ kind: 'blocked', detail: refusal });
      } catch {
        // Aborted, offline, or the probe itself was blocked. The iframe stays;
        // guessing at a cause we have no evidence for would be worse than the
        // browser's own error page.
      }
    })();
    return () => controller.abort();
  }, [iframeSrc, embedded]);

  // Safety guard: if this React app is itself inside an iframe (e.g. the
  // StaticFiles mount isn't registered and serve_spa returned us here),
  // don't render another iframe — that would create an infinite loop.
  if (embedded) {
    return (
      <div style={{ padding: 32, color: '#f88' }}>
        GCode viewer static files not found. Check that the{' '}
        <code>gcode_viewer/</code> directory exists and restart uvicorn.
      </div>
    );
  }

  const cameFromArchive = searchParams.has('archive');
  const cameFromLibrary = searchParams.has('library_file');
  const fallbackPath = cameFromArchive ? '/archives' : cameFromLibrary ? '/files' : '/';
  const backLabel = cameFromArchive
    ? t('gcodeViewer.backToArchives')
    : cameFromLibrary
    ? t('gcodeViewer.backToFiles')
    : t('gcodeViewer.back');

  const handleBack = () => {
    // Prefer browser history so we land where the user actually was (preserving
    // scroll position, filters, etc.). Fall back to a sensible default route
    // when the viewer was opened from a fresh tab / shared link.
    if (window.history.length > 1) {
      navigate(-1);
    } else {
      navigate(fallbackPath);
    }
  };

  return (
    // h-14 (3.5 rem) is the fixed header height defined in Layout.tsx.
    // Subtracting it prevents a double scrollbar inside the layout shell.
    <div style={{ height: 'calc(100vh - 3.5rem)', display: 'flex', flexDirection: 'column' }}>
      <div className="flex items-center gap-2 px-4 py-2 border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
        <button
          type="button"
          onClick={handleBack}
          className="inline-flex items-center gap-1.5 text-sm text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          {backLabel}
        </button>
      </div>
      {problem ? (
        <div className="flex-1 overflow-y-auto p-6">
          <div role="alert" className="max-w-2xl mx-auto p-4 rounded-lg border border-amber-500/40 bg-amber-500/10">
            <div className="flex items-start gap-3">
              <ShieldAlert className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
              <div className="min-w-0">
                <p className="text-sm font-medium text-amber-300">
                  {problem.kind === 'blocked'
                    ? t('gcodeViewer.blockedTitle')
                    : t('gcodeViewer.unavailableTitle')}
                </p>
                <p className="text-xs text-bambu-gray mt-1">
                  {problem.kind === 'blocked'
                    ? t('gcodeViewer.blockedBody')
                    : t('gcodeViewer.unavailableBody')}
                </p>
                <p className="text-xs text-bambu-gray mt-2 font-mono break-all">
                  {t('gcodeViewer.problemDetail', { detail: problem.detail })}
                </p>
                <a
                  href={iframeSrc}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-3 inline-flex items-center gap-1 text-xs text-bambu-green hover:underline"
                >
                  <ExternalLink className="w-3 h-3" />
                  {t('gcodeViewer.openInNewTab')}
                </a>
              </div>
            </div>
          </div>
        </div>
      ) : (
        <iframe
          src={iframeSrc}
          title="GCode Viewer"
          style={{
            display: 'block',
            width: '100%',
            flex: 1,
            border: 'none',
          }}
        />
      )}
    </div>
  );
}
