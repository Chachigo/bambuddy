/**
 * Printer-card body scale (#1848, reporter @misterff1).
 *
 * S/M/L/XL already scaled the card's width, thumbnail and printer name, but
 * every label in the body was pinned at 8-11px, so an XL card carried the same
 * tiny text as an S one. The body now scales too, driven by custom properties
 * on the card root.
 *
 * S and M stay at 1.0 on purpose: an existing install must look identical
 * until the user reaches for a size that is already asking for more space.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockPrinter = {
  id: 1,
  name: 'X1C',
  ip_address: '192.168.1.100',
  serial_number: '01P00A000000001',
  access_code: '12345678',
  model: 'X1C',
  enabled: true,
  nozzle_diameter: 0.4,
  nozzle_type: 'stainless_steel',
  location: 'Workshop',
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const STATUS = {
  connected: true,
  state: 'IDLE',
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -29,
  speed_level: 2,
  ams: [],
  vt_tray: [],
};

let store: Record<string, string>;

/** Render at a given card size and hand back the card root's inline style. */
async function cardStyleAt(cardSize: string) {
  store['printerCardSize'] = cardSize;
  render(<PrintersPage />);
  const card = await waitFor(() => {
    const el = document.getElementById('printer-card-1');
    if (!el) throw new Error('card not rendered');
    return el as HTMLElement;
  });
  return card.style;
}

describe('PrintersPage — printer card body scale (#1848)', () => {
  beforeEach(() => {
    store = {};
    vi.mocked(localStorage.getItem).mockImplementation((key: string) => store[key] ?? null);
    vi.mocked(localStorage.setItem).mockImplementation((key: string, value: string) => {
      store[key] = String(value);
    });
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinter])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(STATUS)),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
    );
  });

  afterEach(() => {
    vi.mocked(localStorage.getItem).mockReset();
    vi.mocked(localStorage.setItem).mockReset();
  });

  it('leaves M — the default — at the sizes shipped before this change', async () => {
    const style = await cardStyleAt('2');

    expect(style.getPropertyValue('--pc-t10')).toBe('10px');
    expect(style.getPropertyValue('--pc-t8')).toBe('8px');
    expect(style.getPropertyValue('--pc-i3')).toBe('12px');
    expect(style.getPropertyValue('--pc-i4')).toBe('16px');
  });

  it('leaves S at the same sizes — the dense fleet view wants density', async () => {
    const style = await cardStyleAt('1');

    expect(style.getPropertyValue('--pc-t10')).toBe('10px');
    expect(style.getPropertyValue('--pc-i3')).toBe('12px');
  });

  it('scales the body type and icons at L', async () => {
    const style = await cardStyleAt('3');

    expect(style.getPropertyValue('--pc-t10')).toBe('12px');
    expect(style.getPropertyValue('--pc-t8')).toBe('9.6px');
    expect(style.getPropertyValue('--pc-t11')).toBe('13.2px');
    expect(style.getPropertyValue('--pc-i3')).toBe('14.4px');
    expect(style.getPropertyValue('--pc-i4')).toBe('19.2px');
  });

  it('scales further at XL, where the card is full width', async () => {
    const style = await cardStyleAt('4');

    expect(style.getPropertyValue('--pc-t10')).toBe('14px');
    expect(style.getPropertyValue('--pc-i4')).toBe('22.4px');
    // Every property is set at every size, so a converted class can never
    // fall through to its fallback while sitting inside a card.
    for (const name of ['--pc-t8', '--pc-t9', '--pc-t10', '--pc-t11',
      '--pc-i2', '--pc-i25', '--pc-i3', '--pc-i35', '--pc-i4', '--pc-i5']) {
      expect(style.getPropertyValue(name)).not.toBe('');
    }
  });

  it('drives real elements, not just the root variables', async () => {
    await cardStyleAt('3');

    // The printer name already scaled before this change and still does.
    const heading = await screen.findByRole('heading', { name: 'X1C' });
    expect(heading.className).toContain('text-xl');

    // Body labels now reference the scaled property rather than a fixed px.
    const scaled = document.querySelectorAll('#printer-card-1 [class*="--pc-t"]');
    expect(scaled.length).toBeGreaterThan(0);
  });
});
