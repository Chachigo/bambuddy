/**
 * The G-code viewer's frame, when something refuses to let it be embedded (#2787).
 *
 * Sliced files preview through a full-page route whose body is an iframe of
 * /gcode-viewer/; STL and source 3MF use an in-page three.js modal instead. So a
 * proxy that injects a framing header breaks exactly one of the two previews,
 * and all the user sees is the browser's own "refused to connect" page inside
 * our layout shell — no clue what happened, and no hint that the viewer works
 * perfectly well in a tab of its own.
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { GCodeViewerPage } from '../../pages/GCodeViewerPage';
import { findFramingRefusal } from '../../utils/framing';
import { server } from '../mocks/server';

const ORIGIN = 'https://printers.example.com';
const OURS = "default-src 'self'; script-src 'self' 'unsafe-eval'; frame-ancestors 'self';";

function serveViewer(status: number, headers: Record<string, string> = {}) {
  server.use(http.get('/gcode-viewer/', () => new HttpResponse(null, { status, headers })));
}

describe('findFramingRefusal', () => {
  it('accepts the headers Bambuddy itself sends', () => {
    expect(findFramingRefusal('SAMEORIGIN', OURS, ORIGIN)).toBeNull();
  });

  it('accepts an origin named explicitly instead of self', () => {
    const csp = `frame-ancestors ${ORIGIN};`;
    expect(findFramingRefusal(null, csp, ORIGIN)).toBeNull();
  });

  it('reports a proxy-added policy that intersects ours down to none', () => {
    // Two Content-Security-Policy headers arrive as one comma-joined string.
    // Both apply, so ours permitting us is not enough.
    const refusal = findFramingRefusal('SAMEORIGIN', `${OURS}, frame-ancestors 'none'`, ORIGIN);
    expect(refusal).toBe("Content-Security-Policy: frame-ancestors 'none'");
  });

  it('reports frame-ancestors listing only somebody else', () => {
    const refusal = findFramingRefusal(null, "frame-ancestors https://ha.example.com;", ORIGIN);
    expect(refusal).toContain('ha.example.com');
  });

  it('reports X-Frame-Options DENY when no frame-ancestors is present', () => {
    expect(findFramingRefusal('DENY', null, ORIGIN)).toBe('X-Frame-Options: DENY');
  });

  it('reports a second X-Frame-Options appended to ours', () => {
    expect(findFramingRefusal('SAMEORIGIN, DENY', null, ORIGIN)).toBe(
      'X-Frame-Options: SAMEORIGIN, DENY',
    );
  });

  it('ignores X-Frame-Options when frame-ancestors permits us, as browsers do', () => {
    // CSP supersedes the legacy header outright — flagging this would blame a
    // header the browser never consulted.
    expect(findFramingRefusal('DENY', OURS, ORIGIN)).toBeNull();
  });

  it('accepts a response carrying no framing headers at all', () => {
    expect(findFramingRefusal(null, null, ORIGIN)).toBeNull();
  });
});

describe('GCodeViewerPage', () => {
  it('embeds the viewer when nothing refuses the frame', async () => {
    serveViewer(200, { 'X-Frame-Options': 'SAMEORIGIN', 'Content-Security-Policy': OURS });

    render(<GCodeViewerPage />);

    expect(await screen.findByTitle('GCode Viewer')).toBeInTheDocument();
    // Give the probe a chance to land and prove it changes nothing.
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(screen.getByTitle('GCode Viewer')).toBeInTheDocument();
  });

  it('explains a refused frame and offers the viewer in its own tab', async () => {
    serveViewer(200, { 'Content-Security-Policy': "frame-ancestors 'none';" });

    render(<GCodeViewerPage />);

    const panel = await screen.findByRole('alert');
    expect(panel).toHaveTextContent(/could not be embedded/i);
    // Name the header so the operator can go and find it in their proxy.
    expect(panel).toHaveTextContent(/frame-ancestors 'none'/);
    // A top-level navigation is not subject to frame-ancestors, so this works.
    const link = within(panel).getByRole('link', { name: /new tab/i });
    expect(link).toHaveAttribute('href', '/gcode-viewer/');
    expect(link).toHaveAttribute('target', '_blank');
    expect(screen.queryByTitle('GCode Viewer')).not.toBeInTheDocument();
  });

  it('reports missing viewer assets rather than showing raw JSON', async () => {
    serveViewer(404);

    render(<GCodeViewerPage />);

    const panel = await screen.findByRole('alert');
    expect(panel).toHaveTextContent(/unavailable/i);
    expect(panel).toHaveTextContent(/HTTP 404/);
    expect(screen.queryByTitle('GCode Viewer')).not.toBeInTheDocument();
  });

  it('keeps the frame when the probe itself fails', async () => {
    // No evidence either way — the browser's own error page is better than a
    // guess at a cause we cannot see.
    server.use(http.get('/gcode-viewer/', () => HttpResponse.error()));

    render(<GCodeViewerPage />);

    expect(await screen.findByTitle('GCode Viewer')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(screen.getByTitle('GCode Viewer')).toBeInTheDocument();
  });
});
