import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { formatETA } from '../../utils/date';
import { formatQueueItemETA } from '../../utils/queueEta';

describe('formatQueueItemETA', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-31T18:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('calculates the ETA from the current time and job duration', () => {
    expect(formatQueueItemETA(90 * 60, '24h')).toBe(
      formatETA(90, '24h'),
    );
  });

  it('returns null without a usable print duration', () => {
    expect(formatQueueItemETA(null)).toBeNull();
    expect(formatQueueItemETA(undefined)).toBeNull();
    expect(formatQueueItemETA(0)).toBeNull();
    expect(formatQueueItemETA(-60)).toBeNull();
  });
});
