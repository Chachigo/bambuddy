import { describe, it, expect, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';

import { render } from '../utils';
import SlicerSettingsPanel from '../../components/SlicerSettingsPanel';
import type { SettingValue } from '../../types/slicerSettings';

/**
 * The panel is a controlled component: it renders from the `values` prop and
 * reports edits upward. Driving it with a bare spy would leave every input
 * frozen at its initial value, so the harness holds state the way SliceModal
 * does and forwards each call to the spy for assertions.
 */
function Harness({
  initial,
  onChange,
}: {
  initial: Record<string, SettingValue>;
  onChange: (v: Record<string, SettingValue>, s: Record<string, string | string[]>) => void;
}) {
  const [values, setValues] = useState(initial);
  return (
    <SlicerSettingsPanel
      values={values}
      onChange={(v, s) => {
        setValues(v);
        onChange(v, s);
      }}
    />
  );
}

/** Renders the panel and waits for its dynamically imported metadata. */
async function renderPanel(initial: Record<string, SettingValue> = {}) {
  const onChange = vi.fn();
  render(<Harness initial={initial} onChange={onChange} />);
  await waitFor(() => expect(screen.getByPlaceholderText('Search settings')).toBeInTheDocument());
  return { onChange };
}

/**
 * Brings one option on screen regardless of which page or visibility tier it
 * belongs to. Searching spans every page, which is how a user would reach a
 * setting they know the name of.
 */
async function showOption(user: ReturnType<typeof userEvent.setup>, label: string, search: string) {
  await user.click(screen.getByRole('button', { name: 'Expert' }));
  const box = screen.getByPlaceholderText('Search settings');
  await user.clear(box);
  await user.type(box, search);
  return waitFor(() => screen.getByLabelText(new RegExp(`^${label}`)));
}

describe('SlicerSettingsPanel', () => {
  it('opens on the first page of the slicer parameter tree', async () => {
    await renderPanel();
    expect(screen.getByRole('button', { name: 'Quality' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Strength' })).toBeInTheDocument();
    expect(screen.getByLabelText(/^Layer height/)).toBeInTheDocument();
  });

  it('reveals more options as the visibility tier widens', async () => {
    const user = userEvent.setup();
    await renderPanel();

    // "Slice gap closing radius" is an advanced-tier Quality option.
    expect(screen.queryByLabelText(/^Slice gap closing radius/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Advanced' }));
    await waitFor(() => expect(screen.getByLabelText(/^Slice gap closing radius/)).toBeInTheDocument());
  });

  it('searches across every page rather than only the open one', async () => {
    const user = userEvent.setup();
    await renderPanel();

    // Enable support lives on the Support page, not the Quality page shown.
    await user.type(screen.getByPlaceholderText('Search settings'), 'enable support');
    await waitFor(() => expect(screen.getByLabelText(/^Enable support/)).toBeInTheDocument());
  });

  it('reports an edit serialised the way a process preset stores it', async () => {
    const user = userEvent.setup();
    const { onChange } = await renderPanel();

    const input = screen.getByLabelText(/^Layer height/);
    await user.clear(input);
    await user.type(input, '0.16');

    await waitFor(() => {
      const [values, serialized] = onChange.mock.calls.at(-1)!;
      expect(values.layer_height).toBe('0.16');
      expect(serialized.layer_height).toBe('0.16');
    });
  });

  it('puts the percent sign back on a percent option', async () => {
    const user = userEvent.setup();
    const { onChange } = await renderPanel();

    const input = await showOption(user, 'Sparse infill density', 'sparse infill density');
    await user.clear(input);
    await user.type(input, '35');

    // "35" and "35%" are different values to the slicer; the schema decides.
    await waitFor(() => {
      const [, serialized] = onChange.mock.calls.at(-1)!;
      expect(serialized.sparse_infill_density).toBe('35%');
    });
  });

  it('sends nothing for a value that equals the preset default', async () => {
    const user = userEvent.setup();
    const { onChange } = await renderPanel();

    // wall_loops defaults to 2 — typing it back is not an override.
    const input = await showOption(user, 'Wall loops', 'wall loops');
    await user.clear(input);
    await user.type(input, '2');

    await waitFor(() => {
      const [values, serialized] = onChange.mock.calls.at(-1)!;
      expect(values.wall_loops).toBe('2');
      expect(serialized).not.toHaveProperty('wall_loops');
    });
  });

  it('greys out options the slicer disables at the current settings', async () => {
    // sparse_infill_density at 0 turns off have_infill, which gates the infill
    // pattern — the same rule the desktop slicer applies.
    const user = userEvent.setup();
    await renderPanel({ sparse_infill_density: '0%' });
    const pattern = await showOption(user, 'Sparse infill pattern', 'sparse infill pattern');
    expect(pattern).toBeDisabled();
  });

  it('keeps an option editable while infill is on', async () => {
    const user = userEvent.setup();
    await renderPanel({ sparse_infill_density: '15%' });
    const pattern = await showOption(user, 'Sparse infill pattern', 'sparse infill pattern');
    expect(pattern).not.toBeDisabled();
  });

  it('lets a field be emptied without snapping back to the default', async () => {
    // Regression: dropping the key on an empty input made the control fall
    // straight back to the preset default, so clearing a value to retype it
    // appended to the old one ("0.2" + "0.16" = "0.2016").
    const user = userEvent.setup();
    await renderPanel();

    const input = screen.getByLabelText(/^Layer height/);
    await user.clear(input);
    expect(input).toHaveValue(null);
  });

  it('clears every override from the header reset', async () => {
    const user = userEvent.setup();
    const { onChange } = await renderPanel({ layer_height: '0.16' });

    await user.click(await screen.findByRole('button', { name: /Reset 1/ }));

    const [values, serialized] = onChange.mock.calls.at(-1)!;
    expect(values).toEqual({});
    expect(serialized).toEqual({});
  });

  it('reverts a single option without touching the others', async () => {
    const user = userEvent.setup();
    const { onChange } = await renderPanel({ layer_height: '0.16', wall_loops: 4 });

    const row = screen.getByLabelText(/^Layer height/).closest('div.group') as HTMLElement;
    await user.click(within(row).getByRole('button', { name: 'Reset to default' }));

    const [values] = onChange.mock.calls.at(-1)!;
    expect(values).not.toHaveProperty('layer_height');
    expect(values.wall_loops).toBe(4);
  });
});
