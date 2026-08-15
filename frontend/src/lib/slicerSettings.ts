/**
 * Conversion between the settings panel's editing values and the string forms
 * OrcaSlicer / BambuStudio write into a process preset JSON.
 *
 * This matters more than it looks. The values we send are merged into the
 * `--load-settings` process JSON, and that JSON is parsed by the slicer CLI,
 * which validates far more strictly than the GUI: a percent option written as
 * `"20"` instead of `"20%"` is a different value, and a bare `true` where the
 * config expects `"1"` fails the parse outright. The panel therefore always
 * serialises through the schema, never by guessing from the JavaScript type.
 */

import type { ProcessOption, ProcessSchema, SettingValue } from '../types/slicerSettings';

/** Option types whose config value is a per-extruder vector. */
const VECTOR_TYPES = new Set(['coBools', 'coFloats', 'coFloatsOrPercents']);

export const isVectorOption = (option: ProcessOption): boolean => VECTOR_TYPES.has(option.type);

/**
 * Numeric bound from the schema, or `undefined` when it isn't a number at all.
 * Float literals are normalised by the generator, but a handful of bounds are
 * unresolved C++ expressions the extractor could not follow, and those must not
 * reach an input's `min`/`max`.
 */
export function numericBound(bound: number | string | undefined): number | undefined {
  if (typeof bound === 'number') return Number.isFinite(bound) ? bound : undefined;
  if (typeof bound !== 'string') return undefined;
  const n = Number.parseFloat(bound);
  return Number.isFinite(n) ? n : undefined;
}

/**
 * A unit suffix worth showing. A few entries carry an unresolved C++ expression
 * where the extractor could not follow a reference (`def_x->sidetext`); showing
 * that to a user would be worse than showing no unit at all.
 */
export function displaySidetext(option: ProcessOption): string | undefined {
  const s = option.sidetext;
  if (!s || s.includes('->') || s.includes('::')) return undefined;
  return s;
}

/** The schema default, rendered the way the panel's inputs want to display it. */
export function defaultForDisplay(option: ProcessOption): string {
  const d = option.default;
  if (d === undefined) return '';
  // Per-extruder vectors render as a comma-separated list. C++ literal
  // artefacts (`0.`, `0.3f`, `100.%`) are normalised by
  // scripts/generate-slicer-schema.mjs, so nothing needs unpicking here.
  if (Array.isArray(d)) return d.map(String).join(', ');
  if (typeof d === 'boolean') return d ? '1' : '0';
  return String(d);
}

/**
 * Serialises one edited value into its process-JSON form.
 *
 * Vector options are written back as arrays because that is how the config
 * stores them; scalars become strings, which is what every Bambu process preset
 * uses even for numeric options.
 */
export function serializeSetting(option: ProcessOption, value: SettingValue): string | string[] {
  if (isVectorOption(option)) {
    const parts = Array.isArray(value) ? value.map(String) : String(value).split(',');
    return parts.map((p) => p.trim()).filter((p) => p !== '');
  }

  if (option.type === 'coBool') {
    if (typeof value === 'boolean') return value ? '1' : '0';
    return value === '1' || value === 'true' || value === 1 ? '1' : '0';
  }

  const raw = String(value).trim();

  if (option.type === 'coPercent') {
    // The config spells percents with the sign; the input edits the number.
    return raw.endsWith('%') ? raw : `${raw}%`;
  }

  return raw;
}

/** Serialises the panel's sparse override map for the slice request. */
export function serializeOverrides(values: Record<string, SettingValue>, schema: ProcessSchema): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {};
  for (const [key, value] of Object.entries(values)) {
    const option = schema[key];
    // A key with no schema entry cannot be serialised correctly, and sending it
    // raw risks a slice failure that is hard to trace back to this panel.
    if (!option) continue;
    out[key] = serializeSetting(option, value);
  }
  return out;
}

/**
 * True when an edited value differs from the option's default. Used to mark
 * modified rows and to decide what is worth sending: an override equal to the
 * default is noise in the process JSON.
 */
export function isModified(option: ProcessOption, value: SettingValue | undefined): boolean {
  if (value === undefined || value === '') return false;
  const serialized = serializeSetting(option, value);
  const asString = Array.isArray(serialized) ? serialized.join(', ') : serialized;

  const d = option.default;
  if (d === undefined) return asString !== '';

  const defaultSerialized = serializeSetting(option, Array.isArray(d) ? d.map(String).join(', ') : (d as SettingValue));
  const defaultString = Array.isArray(defaultSerialized) ? defaultSerialized.join(', ') : defaultSerialized;

  return asString !== defaultString;
}
