/**
 * Tests for the Restore from Git Backup modal (#2656).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { GitHubRestoreModal } from '../../components/GitHubRestoreModal';

const mockCommits = {
  success: true,
  message: 'OK',
  branch: 'main',
  commits: [
    {
      sha: 'aaa1111bbb2222ccc3333ddd4444eee5555ffff0',
      message: 'Bambuddy backup - 2026-07-02 10:00:00 UTC',
      author: 'Bambuddy',
      date: '2026-07-02T10:00:00Z',
    },
    {
      sha: 'bbb2222ccc3333ddd4444eee5555ffff0aaa1111',
      message: 'Bambuddy backup - 2026-07-01 10:00:00 UTC',
      author: 'Bambuddy',
      date: '2026-07-01T10:00:00Z',
    },
  ],
};

const mockPreview = {
  success: true,
  message: 'OK',
  ref: 'aaa1111bbb2222ccc3333ddd4444eee5555ffff0',
  commit: mockCommits.commits[0],
  metadata_version: '1.0',
  categories: [
    { category: 'archives', available: true, item_count: 30, detail: 'Metadata only' },
    { category: 'spools', available: true, item_count: 4, detail: null },
    { category: 'settings', available: true, item_count: 12, detail: null },
    { category: 'kprofiles', available: false, item_count: 0, detail: 'Not present in this backup commit' },
  ],
};

type JsonBody = Record<string, unknown>;

function mockEndpoints(overrides: { preview?: JsonBody; commits?: JsonBody } = {}) {
  server.use(
    http.get('/api/v1/github-backup/commits', () =>
      HttpResponse.json(overrides.commits ?? (mockCommits as unknown as JsonBody))
    ),
    http.get('/api/v1/github-backup/restore/preview', () =>
      HttpResponse.json(overrides.preview ?? (mockPreview as unknown as JsonBody))
    ),
  );
}

describe('GitHubRestoreModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockEndpoints();
  });

  it('renders the title and commit picker', async () => {
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('Restore from Git Backup')).toBeInTheDocument();
    });
    expect(screen.getByLabelText('Backup commit')).toBeInTheDocument();
  });

  it('defaults to the latest commit and lists recent commits', async () => {
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    const select = (await screen.findByLabelText('Backup commit')) as HTMLSelectElement;
    expect(select.value).toBe('HEAD');
    await waitFor(() => {
      expect(screen.getByText(/Latest backup/)).toBeInTheDocument();
    });
    // Commits are labelled by short SHA.
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /aaa1111/ })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: /bbb2222/ })).toBeInTheDocument();
    });
  });

  it('shows item counts for categories present in the commit', async () => {
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('30 in backup')).toBeInTheDocument();
    });
    expect(screen.getByText('4 in backup')).toBeInTheDocument();
    expect(screen.getByText('12 in backup')).toBeInTheDocument();
  });

  it('disables a category that is absent from the commit', async () => {
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('Not present in this backup commit')).toBeInTheDocument();
    });

    const checkboxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    // Four categories in fixed order: archives, spools, settings, kprofiles.
    expect(checkboxes).toHaveLength(4);
    expect(checkboxes[3].disabled).toBe(true);
    expect(checkboxes[0].disabled).toBe(false);
  });

  it('keeps Restore disabled until a category is selected', async () => {
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    const restoreButton = await screen.findByRole('button', { name: /Restore$/ });
    expect(restoreButton).toBeDisabled();

    // Wait for the preview to populate the category list before selecting.
    const checkboxes = await waitFor(() => {
      const found = screen.getAllByRole('checkbox') as HTMLInputElement[];
      expect(found).toHaveLength(4);
      return found;
    });
    await userEvent.click(checkboxes[1]);

    await waitFor(() => expect(restoreButton).not.toBeDisabled());
    expect(screen.getByText('1 selected')).toBeInTheDocument();
  });

  it('requires confirmation before sending the restore', async () => {
    let restoreCalls = 0;
    server.use(
      http.post('/api/v1/github-backup/restore', async () => {
        restoreCalls += 1;
        return HttpResponse.json({
          success: true,
          message: 'Restored 4 item(s) from aaa1111',
          log_id: 3,
          ref: mockPreview.ref,
          results: { spools: { restored: 4, skipped: 1, failed: 0, notes: [] } },
        });
      })
    );
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
    await userEvent.click(checkboxes[1]);
    await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));

    // Confirm dialog appears; nothing sent yet.
    await waitFor(() => {
      expect(screen.getByText('Restore from backup?')).toBeInTheDocument();
    });
    expect(restoreCalls).toBe(0);
  });

  it('sends the selected categories and shows per-category results', async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/github-backup/restore', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          success: true,
          message: 'Restored 4 item(s) from aaa1111',
          log_id: 3,
          ref: mockPreview.ref,
          results: {
            spools: { restored: 4, skipped: 1, failed: 0, notes: ['1 usage record(s) skipped'] },
          },
        });
      })
    );
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
    await userEvent.click(checkboxes[1]);
    await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));
    await waitFor(() => screen.getByText('Restore from backup?'));

    const confirmButtons = screen.getAllByRole('button', { name: /Restore$/ });
    await userEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => {
      expect(screen.getByText('Restored 4 item(s) from aaa1111')).toBeInTheDocument();
    });
    // The commit posted is the sha the preview resolved to, not the symbolic
    // 'HEAD' the picker defaults to: re-resolving server-side would restore a
    // backup that landed after the preview the user actually approved.
    expect(body).toMatchObject({
      categories: ['spools'],
      overwrite_existing: false,
      ref: mockPreview.ref,
    });
    expect(screen.getByText('4 restored, 1 skipped, 0 failed')).toBeInTheDocument();
    expect(screen.getByText('1 usage record(s) skipped')).toBeInTheDocument();
  });

  it('sends overwrite_existing when the toggle is on', async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/github-backup/restore', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ success: true, message: 'done', log_id: 1, ref: 'x', results: {} });
      })
    );
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
    await userEvent.click(checkboxes[1]);
    await userEvent.click(screen.getByRole('switch'));
    await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));
    await waitFor(() => screen.getByText('Restore from backup?'));
    const confirmButtons = screen.getAllByRole('button', { name: /Restore$/ });
    await userEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => expect(body).toMatchObject({ overwrite_existing: true }));
  });

  it('warns more strongly when overwrite is enabled', async () => {
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
    await userEvent.click(checkboxes[1]);
    await userEvent.click(screen.getByRole('switch'));
    await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));

    await waitFor(() => {
      expect(screen.getByText(/This cannot be undone/)).toBeInTheDocument();
    });
  });

  it('surfaces a preview failure instead of an empty category list', async () => {
    mockEndpoints({
      preview: {
        success: false,
        message: 'Commit or tree deadbee not found in the repository',
        ref: 'deadbee',
        categories: [],
      },
    });
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('Commit or tree deadbee not found in the repository')).toBeInTheDocument();
    });
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('surfaces a commit listing failure', async () => {
    mockEndpoints({
      commits: { success: false, message: 'Invalid access token', branch: 'main', commits: [] },
    });
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('Invalid access token')).toBeInTheDocument();
    });
  });

  // A refused restore answers 200 with `success: false`, and two of the five
  // refusals are ordinary conditions rather than errors — a restore already
  // running, and a backup mid-flight. Rendering the result panel for those put a
  // green tick and "reload so the restored data appears" above a message saying
  // nothing had been restored, i.e. a failure that read as a success.
  it('reports a backend refusal such as the backup/restore mutex', async () => {
    server.use(
      http.post('/api/v1/github-backup/restore', () =>
        HttpResponse.json({
          success: false,
          message: 'A backup is currently running. Wait for it to finish before restoring.',
          results: {},
        })
      )
    );
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
    await userEvent.click(checkboxes[1]);
    await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));
    await waitFor(() => screen.getByText('Restore from backup?'));
    const confirmButtons = screen.getAllByRole('button', { name: /Restore$/ });
    await userEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => {
      expect(
        screen.getByText('A backup is currently running. Wait for it to finish before restoring.')
      ).toBeInTheDocument();
    });

    // Not the success panel: no reload hint, no "Reload now", and the form is
    // still there so the user can retry once the backup finishes.
    expect(screen.queryByText(/Reload Bambuddy so the restored data appears/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Reload now/ })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Backup commit')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Restore$/ })).toBeEnabled();
  });

  it('does not refresh the data caches when a restore was refused', async () => {
    server.use(
      http.post('/api/v1/github-backup/restore', () =>
        HttpResponse.json({ success: false, message: 'A restore is already running', results: {} })
      )
    );
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries');
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
    await userEvent.click(checkboxes[1]);
    await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));
    await waitFor(() => screen.getByText('Restore from backup?'));
    const confirmButtons = screen.getAllByRole('button', { name: /Restore$/ });
    await userEvent.click(confirmButtons[confirmButtons.length - 1]);
    await waitFor(() => screen.getByText('A restore is already running'));

    const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    // Nothing was written, so nothing to re-read...
    expect(keys).not.toContain(JSON.stringify(['spools']));
    expect(keys).not.toContain(JSON.stringify(['archives']));
    // ...but a failure past the commit resolve writes a "failed" log row, so the
    // history is refreshed whatever the outcome.
    expect(keys).toContain(JSON.stringify(['github-backup-logs']));
    invalidate.mockRestore();
  });

  // A provider-side failure answers 200 with `success: false`; a rejected
  // *request* throws in `request()`, leaving `data` undefined. Reading the
  // message off `data` alone meant the second kind rendered an empty modal —
  // picker holding only "Latest", every category greyed out, no explanation.
  it('explains a rejected preview request instead of greying out every category', async () => {
    server.use(
      http.get('/api/v1/github-backup/restore/preview', () =>
        HttpResponse.json({ detail: 'Not authenticated' }, { status: 401 })
      )
    );
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('Not authenticated')).toBeInTheDocument();
    });
    // The category list is replaced by the error, not rendered disabled.
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('explains a rejected commit-list request', async () => {
    server.use(
      http.get('/api/v1/github-backup/commits', () => HttpResponse.json({}, { status: 500 }))
    );
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    // No detail in the body, so the generic string carries the message.
    await waitFor(() => {
      expect(screen.getByText(/Could not read the backup repository|HTTP 500/)).toBeInTheDocument();
    });
  });

  it('closes via the close button', async () => {
    const onClose = vi.fn();
    render(<GitHubRestoreModal onClose={onClose} />);

    await waitFor(() => screen.getByText('Restore from Git Backup'));
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));

    expect(onClose).toHaveBeenCalled();
  });
});
