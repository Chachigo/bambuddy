import { formatETA, type TimeFormat } from './date';

/**
 * Formats the estimated completion time for a queue item if it were
 * started at the current time.
 *
 * This is deliberately a per-job estimate rather than a cumulative
 * queue forecast.
 */
export function formatQueueItemETA(
  printTimeSeconds: number | null | undefined,
  timeFormat: TimeFormat = 'system',
  t?: Parameters<typeof formatETA>[2],
): string | null {
  if (printTimeSeconds == null || printTimeSeconds <= 0) return null;

  return formatETA(printTimeSeconds / 60, timeFormat, t);
}
