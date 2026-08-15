/**
 * Process-settings editor mirroring OrcaSlicer's own Print Settings tabs.
 *
 * Structure, labels, tooltips, bounds, defaults and enable/disable rules all
 * come from metadata extracted from OrcaSlicer's C++ sources (see
 * `src/data/slicer/`), so the pages, groups and ordering match what users see
 * in the desktop slicer rather than a hand-picked subset.
 *
 * Option labels and tooltips are deliberately English-only for now: they are
 * 348 strings lifted verbatim from `PrintConfig.cpp`, and hand-translating them
 * into all 13 locales is not viable. The panel's own chrome — mode switch,
 * search, buttons, empty states — goes through i18n as usual. OrcaSlicer ships
 * its own translation catalogs for these strings, which is the obvious source
 * if they are ever picked up.
 *
 * Values are held sparsely: only options the user actually changed are tracked
 * and sent, so a slice with an untouched panel is byte-identical to one from
 * before this panel existed.
 */

import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Search, RotateCcw, Loader2 } from 'lucide-react';

import { disabledKeys, type ToggleRules } from '../lib/slicerToggle';
import { defaultForDisplay, displaySidetext, isModified, numericBound, serializeOverrides } from '../lib/slicerSettings';
import type { OptionMode, ProcessOption, ProcessSchema, ProcessUiTree, SettingValue } from '../types/slicerSettings';

interface SlicerData {
  schema: ProcessSchema;
  tree: ProcessUiTree;
  toggles: ToggleRules;
}

interface Props {
  values: Record<string, SettingValue>;
  /**
   * Reports both the panel's editing state and the same values serialised for
   * the slice request. Serialising here rather than in the caller keeps the
   * option schema — the only thing that knows a percent needs its `%` back —
   * in the one component that has already loaded it.
   *
   * `serialized` carries only options that actually differ from their default,
   * so an untouched panel sends nothing at all.
   */
  onChange: (values: Record<string, SettingValue>, serialized: Record<string, string | string[]>) => void;
  disabled?: boolean;
}

/** Visibility tiers, in increasing order of how much they reveal. */
const MODES: OptionMode[] = ['simple', 'advanced', 'expert'];
const MODE_RANK: Record<string, number> = { simple: 0, advanced: 1, expert: 2, develop: 3 };

export default function SlicerSettingsPanel({ values, onChange, disabled = false }: Props) {
  const { t } = useTranslation();
  const [data, setData] = useState<SlicerData | null>(null);
  const [mode, setMode] = useState<OptionMode>('simple');
  const [page, setPage] = useState<string | null>(null);
  const [query, setQuery] = useState('');

  // 150 KB of extracted metadata has no business in the main bundle — it is
  // only needed once someone opens this panel.
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      import('../data/slicer/process-schema.json'),
      import('../data/slicer/process-ui-tree.json'),
      import('../data/slicer/process-toggle-rules.json'),
    ]).then(([schema, tree, toggles]) => {
      if (cancelled) return;
      setData({
        schema: (schema.default ?? schema) as unknown as ProcessSchema,
        tree: (tree.default ?? tree) as unknown as ProcessUiTree,
        toggles: (toggles.default ?? toggles) as unknown as ToggleRules,
      });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const off = useMemo(
    () => (data ? disabledKeys(values, data.schema, data.toggles) : new Set<string>()),
    [data, values],
  );

  const emit = (next: Record<string, SettingValue>) => {
    if (!data) return;
    // Only genuine deviations are worth sending: an override that equals the
    // preset's own value is noise in the process JSON and makes the slice
    // request harder to read when something goes wrong.
    const changed: Record<string, SettingValue> = {};
    for (const [k, v] of Object.entries(next)) {
      if (data.schema[k] && isModified(data.schema[k], v)) changed[k] = v;
    }
    onChange(next, serializeOverrides(changed, data.schema));
  };

  const setValue = (key: string, value: SettingValue | undefined) => {
    const next = { ...values };
    if (value === undefined) delete next[key];
    else next[key] = value;
    emit(next);
  };

  // Search cuts across every page; without a query we show the selected page.
  const visiblePages = useMemo(() => {
    if (!data) return [];
    const needle = query.trim().toLowerCase();
    const withinMode = (key: string) => MODE_RANK[data.schema[key]?.mode ?? 'expert'] <= MODE_RANK[mode];
    const matches = (key: string) => {
      if (!needle) return true;
      const opt = data.schema[key];
      return key.includes(needle) || opt?.label?.toLowerCase().includes(needle) || opt?.tooltip?.toLowerCase().includes(needle);
    };

    return data.tree
      .map((p) => ({
        ...p,
        groups: p.groups
          .map((g) => ({ ...g, options: g.options.filter((k) => withinMode(k) && matches(k)) }))
          .filter((g) => g.options.length > 0),
      }))
      .filter((p) => p.groups.length > 0);
  }, [data, mode, query]);

  const activePage = useMemo(() => {
    if (visiblePages.length === 0) return null;
    if (query.trim()) return null; // Searching shows every match, not one page.
    return visiblePages.find((p) => p.page === page) ?? visiblePages[0];
  }, [visiblePages, page, query]);

  const modifiedCount = useMemo(() => {
    if (!data) return 0;
    return Object.keys(values).filter((k) => data.schema[k] && isModified(data.schema[k], values[k])).length;
  }, [data, values]);

  if (!data) {
    return (
      <div className="flex items-center justify-center gap-2 py-8 text-sm text-bambu-gray">
        <Loader2 className="w-4 h-4 animate-spin" />
        {t('slicerSettings.loading', 'Loading slicer settings...')}
      </div>
    );
  }

  const shownPages = activePage ? [activePage] : visiblePages;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex rounded overflow-hidden border border-white/10">
          {MODES.map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              disabled={disabled}
              className={`px-2.5 py-1 text-xs capitalize transition-colors ${
                mode === m ? 'bg-bambu-green text-black' : 'text-bambu-gray hover:text-white'
              }`}
            >
              {t(`slicerSettings.mode.${m}`, m)}
            </button>
          ))}
        </div>

        <div className="relative flex-1 min-w-[10rem]">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-bambu-gray" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            disabled={disabled}
            placeholder={t('slicerSettings.searchPlaceholder', 'Search settings')}
            className="w-full bg-black/30 border border-white/10 rounded pl-7 pr-2 py-1 text-xs text-white placeholder:text-bambu-gray/60"
          />
        </div>

        {modifiedCount > 0 && (
          <button
            type="button"
            onClick={() => emit({})}
            disabled={disabled}
            className="flex items-center gap-1 text-xs text-bambu-gray hover:text-white"
          >
            <RotateCcw className="w-3 h-3" />
            {t('slicerSettings.resetAll', 'Reset {{count}}', { count: modifiedCount })}
          </button>
        )}
      </div>

      {!query.trim() && (
        <div className="flex flex-wrap gap-1">
          {visiblePages.map((p) => (
            <button
              key={p.page}
              type="button"
              onClick={() => setPage(p.page)}
              disabled={disabled}
              className={`px-2 py-1 text-xs rounded transition-colors ${
                activePage?.page === p.page ? 'bg-white/10 text-white' : 'text-bambu-gray hover:text-white'
              }`}
            >
              {p.page}
            </button>
          ))}
        </div>
      )}

      {shownPages.length === 0 ? (
        <p className="py-6 text-center text-xs text-bambu-gray">
          {t('slicerSettings.noMatches', 'No settings match this search.')}
        </p>
      ) : (
        <div className="flex flex-col gap-4 max-h-[22rem] overflow-y-auto pr-1">
          {shownPages.map((p) => (
            <div key={p.page} className="flex flex-col gap-3">
              {query.trim() && <p className="text-[0.7rem] uppercase tracking-wide text-bambu-gray/70">{p.page}</p>}
              {p.groups.map((g) => (
                <fieldset key={`${p.page}:${g.group}`} className="flex flex-col gap-1.5">
                  <legend className="text-xs font-medium text-white/80 mb-1">{g.group}</legend>
                  {g.options.map((key) => (
                    <OptionRow
                      key={key}
                      optionKey={key}
                      option={data.schema[key]}
                      value={values[key]}
                      onChange={(v) => setValue(key, v)}
                      disabled={disabled || off.has(key)}
                      disabledBySlicer={off.has(key)}
                    />
                  ))}
                </fieldset>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

interface RowProps {
  optionKey: string;
  option: ProcessOption;
  value: SettingValue | undefined;
  onChange: (value: SettingValue | undefined) => void;
  disabled: boolean;
  /** Greyed because the slicer's own rules turn it off, not because the form is busy. */
  disabledBySlicer: boolean;
}

function OptionRow({ optionKey, option, value, onChange, disabled, disabledBySlicer }: RowProps) {
  const { t } = useTranslation();
  const modified = isModified(option, value);
  const unit = displaySidetext(option);
  const current = value === undefined ? defaultForDisplay(option) : String(value);

  return (
    <div className="flex items-center gap-2 group" title={option.tooltip}>
      <label
        htmlFor={`slicer-opt-${optionKey}`}
        className={`flex-1 text-xs truncate ${disabledBySlicer ? 'text-bambu-gray/40' : 'text-bambu-gray'}`}
      >
        {option.label || optionKey}
        {modified && <span className="ml-1 text-bambu-green" aria-hidden="true">•</span>}
      </label>

      <div className="flex items-center gap-1 shrink-0">
        <OptionControl
          id={`slicer-opt-${optionKey}`}
          option={option}
          current={current}
          onChange={onChange}
          disabled={disabled}
        />
        {unit && <span className="text-[0.65rem] text-bambu-gray/60 w-10 truncate">{unit}</span>}
        <button
          type="button"
          onClick={() => onChange(undefined)}
          disabled={disabled || !modified}
          aria-label={t('slicerSettings.resetOption', 'Reset to default')}
          className={`p-0.5 transition-opacity ${modified ? 'text-bambu-gray hover:text-white' : 'opacity-0 pointer-events-none'}`}
        >
          <RotateCcw className="w-3 h-3" />
        </button>
      </div>
    </div>
  );
}

interface ControlProps {
  id: string;
  option: ProcessOption;
  current: string;
  onChange: (value: SettingValue | undefined) => void;
  disabled: boolean;
}

function OptionControl({ id, option, current, onChange, disabled }: ControlProps) {
  const inputClass = 'bg-black/30 border border-white/10 rounded px-1.5 py-0.5 text-xs text-white disabled:opacity-40 w-24';

  if (option.type === 'coBool') {
    return (
      <input
        id={id}
        type="checkbox"
        checked={current === '1' || current === 'true'}
        onChange={(e) => onChange(e.target.checked)}
        disabled={disabled}
        className="w-3.5 h-3.5 cursor-pointer disabled:opacity-40"
      />
    );
  }

  if (option.type === 'coEnum' && option.enum_values) {
    return (
      <select
        id={id}
        value={current}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className={inputClass}
      >
        {option.enum_values.map((v, i) => (
          <option key={v} value={v}>
            {option.enum_labels?.[i] ?? v}
          </option>
        ))}
      </select>
    );
  }

  if (option.type === 'coInt' || option.type === 'coFloat' || option.type === 'coPercent') {
    return (
      <input
        id={id}
        type="number"
        value={current.replace('%', '')}
        min={numericBound(option.min)}
        max={numericBound(option.max)}
        step={option.type === 'coInt' ? 1 : 'any'}
        // An empty field is kept as an empty string rather than dropped.
        // Dropping it would fall the input straight back to the default, so
        // clearing a value to retype it would silently append to the old one.
        // Empty never counts as modified, so nothing is sent for it either way;
        // the revert button is what actually removes the key.
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className={inputClass}
      />
    );
  }

  // coFloatOrPercent, the vector types and coString all accept free text: they
  // hold values like "50%", "0.42" or a comma-separated per-extruder list, none
  // of which a number input can represent.
  return (
    <input
      id={id}
      type="text"
      value={current}
      onChange={(e) => onChange(e.target.value === '' ? undefined : e.target.value)}
      disabled={disabled}
      className={inputClass}
    />
  );
}
