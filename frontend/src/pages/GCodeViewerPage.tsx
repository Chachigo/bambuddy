import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { api } from '../api/client';
import { GcodeToolpathViewer } from '../components/GcodeToolpathViewer';

/**
 * Full-page G-code preview.
 *
 * Previously an iframe onto a vendored copy of PrettyGCode served from
 * `/gcode-viewer/`. That brought its own problems -- a second viewer to keep
 * packaged and updated, no way to theme or translate it, and a whole
 * frame-refusal probe to detect when a proxy blocked the embed -- and its
 * output was the thing this page exists to show.
 *
 * It now renders Bambuddy's own toolpath viewer, which draws with OrcaSlicer's
 * `libvgcode` and colours by feature. Same component as the file-manager
 * preview, so the two surfaces cannot drift apart.
 */
export function GCodeViewerPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { t } = useTranslation();

  const archiveId = searchParams.get('archive');
  const libraryFileId = searchParams.get('library_file');
  const plate = searchParams.get('plate');

  // Filament colours, so a multi-material print opens on its own colours.
  // The two sources differ: an archive reports them through its capabilities,
  // while a library file carries them in its plate metadata, read straight out
  // of the 3MF's slice info. Neither is worth blocking the preview over -- the
  // viewer falls back to feature colouring -- hence no retry and no error path.
  const archiveColorsQuery = useQuery({
    queryKey: ['gcode-viewer-archive-colors', archiveId],
    queryFn: () => api.getArchiveCapabilities(Number(archiveId)),
    enabled: Boolean(archiveId),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const libraryPlatesQuery = useQuery({
    queryKey: ['gcode-viewer-library-colors', libraryFileId],
    queryFn: () => api.getLibraryFilePlates(Number(libraryFileId)),
    enabled: Boolean(libraryFileId),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const filamentColors = useMemo<string[] | undefined>(() => {
    if (archiveId) return archiveColorsQuery.data?.filament_colors;

    const plates = libraryPlatesQuery.data?.plates ?? [];
    // Colours are per plate; use the one being previewed.
    const wanted = plate ? Number(plate) : null;
    const source = (wanted != null && plates.find((p) => p.index === wanted)) || plates[0];
    if (!source?.filaments?.length) return undefined;

    // slot_id is 1-based and the G-code's tool numbers are 0-based, so index
    // by slot - 1 or every colour lands one filament out.
    const colors: string[] = [];
    for (const filament of source.filaments) {
      const slot = Math.max(0, (filament.slot_id ?? 1) - 1);
      if (filament.color) colors[slot] = filament.color;
    }
    return colors.length > 0 ? colors : undefined;
  }, [archiveId, archiveColorsQuery.data, libraryPlatesQuery.data, plate]);

  const gcodeUrl = useMemo(() => {
    // Multi-plate sources need the plate carried through, or the viewer shows
    // whichever plate the backend defaults to rather than the one picked.
    const withPlate = (base: string) => (plate ? `${base}?plate=${encodeURIComponent(plate)}` : base);
    if (archiveId) return withPlate(api.getArchiveGcode(Number(archiveId)));
    if (libraryFileId) return withPlate(api.getLibraryFileGcodeUrl(Number(libraryFileId)));
    return null;
  }, [archiveId, libraryFileId, plate]);

  const handleBack = () => {
    if (window.history.length > 1) navigate(-1);
    else navigate(archiveId ? '/archives' : '/files');
  };

  const backLabel = archiveId
    ? t('gcodeViewer.backToArchives', 'Back to Archives')
    : t('gcodeViewer.backToFiles', 'Back to File Manager');

  return (
    <div className="flex flex-col h-full">
      <div className="flex-shrink-0 px-4 py-2 border-b border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={handleBack}
          className="inline-flex items-center gap-1.5 text-sm text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          {backLabel}
        </button>
      </div>

      {gcodeUrl ? (
        <GcodeToolpathViewer
          gcodeUrl={gcodeUrl}
          filamentColors={filamentColors}
          className="flex-1 min-h-0"
        />
      ) : (
        <div className="flex-1 flex items-center justify-center text-sm text-bambu-gray">
          {t('gcodeViewer.noSource', 'No file was given to preview.')}
        </div>
      )}
    </div>
  );
}
