/**
 * Reading a response's framing headers, for the embedded G-code viewer (#2787).
 *
 * The viewer is the only part of Bambuddy that embeds a Bambuddy page in a
 * frame, so it is the only part a proxy-added framing header can break — and it
 * breaks with the browser's own error page, which says nothing about what was
 * refused or by whom.
 */

/** Why the viewer could not be shown inline, with the evidence that says so. */
export type FrameProblem =
  | { kind: 'blocked'; detail: string }
  | { kind: 'unavailable'; detail: string };

/**
 * Decide whether a response's framing headers allow `origin` to embed it.
 *
 * Returns the offending header verbatim when embedding is refused, or null when
 * it is allowed. Bambuddy's own headers always allow it (`frame-ancestors
 * 'self'` plus `X-Frame-Options: SAMEORIGIN`, set in `main.py`), so a refusal
 * means something between the browser and Bambuddy — a reverse proxy, a
 * security add-on, an auth gateway — added a stricter one.
 *
 * `frame-ancestors` wins outright when present: per CSP the browser must ignore
 * `X-Frame-Options` entirely in that case, so reading both would blame a
 * proxy-added `X-Frame-Options: DENY` the browser never consulted. Multiple CSP
 * headers are *intersected*, and `fetch` joins them into one comma-separated
 * string, so every `frame-ancestors` occurrence has to permit us — not just the
 * first one.
 */
export function findFramingRefusal(
  xFrameOptions: string | null,
  contentSecurityPolicy: string | null,
  origin: string,
): string | null {
  const csp = contentSecurityPolicy ?? '';
  const directives = [...csp.matchAll(/(?:^|[;,])\s*frame-ancestors\s+([^;,]*)/gi)];
  if (directives.length > 0) {
    const self = origin.toLowerCase();
    for (const [, raw] of directives) {
      const value = raw.trim();
      const sources = value.toLowerCase().split(/\s+/).filter(Boolean);
      const permitsUs = sources.some(
        (source) =>
          source === '*' ||
          source === "'self'" ||
          source === self ||
          source === self.replace(/^https?:\/\//, ''),
      );
      if (!permitsUs) return `Content-Security-Policy: frame-ancestors ${value}`;
    }
    return null;
  }

  // No frame-ancestors anywhere: the legacy header governs. Anything other than
  // a single SAMEORIGIN refuses us — DENY, ALLOW-FROM, or the conflicting
  // "SAMEORIGIN, DENY" that appears when a proxy appends a second copy.
  const legacy = (xFrameOptions ?? '')
    .split(',')
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);
  if (legacy.length === 0) return null;
  if (legacy.length === 1 && legacy[0] === 'sameorigin') return null;
  return `X-Frame-Options: ${xFrameOptions}`;
}
