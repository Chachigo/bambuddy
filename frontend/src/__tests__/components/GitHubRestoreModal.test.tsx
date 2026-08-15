/**
 * Tests for the Restore from Git Backup modal (#2656).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { delay, http, HttpResponse } from 'msw';
import { QueryClient, useQuery, useQueryClient } from '@tanstack/react-query';
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
  // The server describes each caveat as a code plus typed params, carrying the
  // English rendering as `detail` for i18next's defaultValue (#2656). Note the
  // fixture's English deliberately differs from en.ts, so an assertion on the
  // locale string proves the code was translated rather than echoed.
  categories: [
    {
      category: 'archives',
      available: true,
      item_count: 30,
      detail: 'raw server English, should not be rendered',
      detail_code: 'archivesMetadataOnly',
      detail_params: {},
    },
    { category: 'spools', available: true, item_count: 4, detail: null, detail_code: null, detail_params: {} },
    { category: 'settings', available: true, item_count: 12, detail: null, detail_code: null, detail_params: {} },
    {
      category: 'kprofiles',
      available: false,
      item_count: 0,
      detail: 'raw server English, should not be rendered',
      detail_code: 'notPresent',
      detail_params: {},
    },
  ],
};

// The default fixture has no K-profiles in the commit, which is the one category
// whose row cannot be selected there.
const mockPreviewWithKprofiles = {
  ...mockPreview,
  categories: mockPreview.categories.map((c) =>
    c.category === 'kprofiles'
      ? { category: 'kprofiles', available: true, item_count: 3, detail: null, detail_code: null, detail_params: {} }
      : c
  ),
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

  it('translates preview caveats rather than echoing the server English', async () => {
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    await waitFor(() => {
      expect(
        screen.getByText('Metadata only - 3MF files and thumbnails are not in a Git backup')
      ).toBeInTheDocument();
    });
    expect(screen.queryAllByText('raw server English, should not be rendered')).toHaveLength(0);
  });

  it('falls back to the server English for a code it does not know', async () => {
    // A newer backend adding a detail_code this build has no key for must not
    // print the raw key at the user. Same defaultValue arm backup.pathCheck uses.
    mockEndpoints({
      preview: {
        ...mockPreview,
        categories: [
          {
            category: 'spools',
            available: true,
            item_count: 4,
            detail: 'Something a future release explains',
            detail_code: 'somethingThisBuildHasNeverHeardOf',
            detail_params: {},
          },
        ],
      },
    });
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('Something a future release explains')).toBeInTheDocument();
    });
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
            spools: {
              restored: 4,
              skipped: 1,
              failed: 0,
              notes: [
                {
                  code: 'spoolUsageUnresolved',
                  params: { count: 1 },
                  message: 'raw server English, should not be rendered',
                },
              ],
            },
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
    // The locale string with {{count}} filled in, not the server's English —
    // which is what makes the note translatable for a non-English user.
    expect(
      screen.getByText(/^1 usage record\(s\) skipped - their spool is not in this backup's spool list/)
    ).toBeInTheDocument();
    expect(screen.queryByText('raw server English, should not be rendered')).not.toBeInTheDocument();
  });

  it('drops the selection while a newly-picked commit is still being inspected', async () => {
    // Switching commits keeps `selected` (it is only pruned once the new preview
    // lands), so the footer must not keep counting it: the categories belong to
    // the commit that was switched away from, and the user has not seen an item
    // count for the new one.
    let previewCalls = 0;
    server.use(
      http.get('/api/v1/github-backup/restore/preview', async () => {
        previewCalls += 1;
        // The second commit's preview never resolves, holding the modal in the
        // in-flight state the assertions below describe.
        if (previewCalls > 1) await delay('infinite');
        return HttpResponse.json(mockPreview as unknown as JsonBody);
      })
    );
    render(<GitHubRestoreModal onClose={vi.fn()} />);

    const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
    await userEvent.click(checkboxes[1]);
    await waitFor(() => expect(screen.getByText('1 selected')).toBeInTheDocument());

    await userEvent.selectOptions(screen.getByLabelText('Backup commit'), mockCommits.commits[1].sha);

    await waitFor(() => expect(screen.getByText('Reading backup contents...')).toBeInTheDocument());
    expect(screen.getByText('0 selected')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Restore$/ })).toBeDisabled();
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

  // Overwrite-off says existing entries stay as they are. K-profiles are the one
  // category that cannot honour that — writing a slot always replaces the
  // calibration on the printer — and the backend's note saying so only arrives
  // in the result panel, after the MQTT send. So the disclosure has to be on the
  // screen where the promise is made, before the user commits to it.
  describe('the K-profile exception to overwrite-off', () => {
    beforeEach(() => {
      mockEndpoints({ preview: mockPreviewWithKprofiles as unknown as JsonBody });
    });

    it('appears beside the category as soon as it is selected', async () => {
      render(<GitHubRestoreModal onClose={vi.fn()} />);

      const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
      await userEvent.click(checkboxes[3]);

      await waitFor(() => {
        expect(screen.getByText(/K-profiles are the exception/)).toBeInTheDocument();
      });

      // And it goes once overwrite is on, where nothing is promising otherwise.
      await userEvent.click(screen.getByRole('switch'));
      expect(screen.queryByText(/K-profiles are the exception/)).not.toBeInTheDocument();
    });

    it('is part of the confirmation the user actually clicks through', async () => {
      render(<GitHubRestoreModal onClose={vi.fn()} />);

      const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
      await userEvent.click(checkboxes[3]);
      await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));

      await waitFor(() => screen.getByText('Restore from backup?'));
      expect(
        screen.getByText(/existing entries stay as they are\. K-profiles are the exception/)
      ).toBeInTheDocument();
    });

    it('stays out of the confirmation for the categories that do keep the promise', async () => {
      render(<GitHubRestoreModal onClose={vi.fn()} />);

      const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
      await userEvent.click(checkboxes[1]);
      await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));

      await waitFor(() => screen.getByText('Restore from backup?'));
      expect(screen.getByText(/existing entries stay as they are\.$/)).toBeInTheDocument();
      expect(screen.queryByText(/K-profiles are the exception/)).not.toBeInTheDocument();
    });

    it('is redundant with overwrite on, so it is not shown there', async () => {
      render(<GitHubRestoreModal onClose={vi.fn()} />);

      const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
      await userEvent.click(checkboxes[3]);
      await userEvent.click(screen.getByRole('switch'));
      await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));

      await waitFor(() => screen.getByText(/This cannot be undone/));
      expect(screen.queryByText(/K-profiles are the exception/)).not.toBeInTheDocument();
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

  // Regression guard for the settings clobber found in manual testing (#2656).
  // SettingsPage — which renders this modal — holds a `localSettings` copy of the
  // form state and a debounced effect that PATCHes it back whenever the server
  // copy differs. Refetching ['settings'] here therefore made the page overwrite
  // the restore with the pre-restore values, silently, ~500 ms later. 75 of the
  // ~80 keys in a real backup sit in that save payload, so a restore reported as
  // "77 restored, 0 failed" left almost nothing behind.
  describe('settings restore must not be undone by SettingsPage', () => {
    /** Runs a restore returning `results`, leaving the modal on its summary. */
    async function restoreWith(results: Record<string, unknown>, onClose = vi.fn()) {
      server.use(
        http.post('/api/v1/github-backup/restore', () =>
          HttpResponse.json({
            success: true,
            message: 'Restored 77 item(s) from aaa1111',
            log_id: 7,
            ref: mockPreview.ref,
            results,
          })
        )
      );
      render(<GitHubRestoreModal onClose={onClose} />);

      const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
      await userEvent.click(checkboxes[1]);
      await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));
      await waitFor(() => screen.getByText('Restore from backup?'));
      const confirmButtons = screen.getAllByRole('button', { name: /Restore$/ });
      await userEvent.click(confirmButtons[confirmButtons.length - 1]);
      await waitFor(() => screen.getByText('Restored 77 item(s) from aaa1111'));
      return onClose;
    }

    /** Replaces window.location with a reload spy for the duration of a test. */
    function stubReload() {
      const original = window.location;
      const reload = vi.fn();
      Object.defineProperty(window, 'location', {
        configurable: true,
        value: { ...original, reload },
      });
      return {
        reload,
        restore: () =>
          Object.defineProperty(window, 'location', { configurable: true, value: original }),
      };
    }

    /**
     * Stands in for SettingsPage: hands the test the provider's QueryClient and
     * keeps an observer on ['settings'] for as long as the modal is mounted, so
     * the entry survives the test client's `gcTime: 0`. Disabled, because these
     * tests write the cache directly rather than fetching it.
     */
    function makeSettingsProbe(capture: (client: QueryClient) => void) {
      return function SettingsProbe() {
        capture(useQueryClient());
        useQuery({ queryKey: ['settings'], queryFn: async () => null, enabled: false });
        return null;
      };
    }

    it('never invalidates the settings query', async () => {
      const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries');
      await restoreWith({ settings: { restored: 77, skipped: 3, failed: 0, notes: [] } });

      const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
      // The caches this restore genuinely rewrites are still refreshed...
      expect(keys).toContain(JSON.stringify(['spools']));
      // ...but ['settings'] must not be, or the page writes the old values back.
      expect(keys).not.toContain(JSON.stringify(['settings']));
      invalidate.mockRestore();
    });

    it('reloads instead of merely closing after a settings restore', async () => {
      const loc = stubReload();
      try {
        const onClose = await restoreWith({
          settings: { restored: 77, skipped: 3, failed: 0, notes: [] },
        });

        const closeButtons = screen.getAllByRole('button', { name: 'Close' });
        await userEvent.click(closeButtons[closeButtons.length - 1]);

        expect(loc.reload).toHaveBeenCalled();
        // Closing in place would leave SettingsPage mounted and armed.
        expect(onClose).not.toHaveBeenCalled();
      } finally {
        loc.restore();
      }
    });

    // Not invalidating ['settings'] only closes the refetch *this* modal caused.
    // The query still refetches on window focus and on reconnect — both default
    // to true, and ['settings'] has other always-mounted observers — and a
    // refetch landing while the result panel is up puts the restored values in
    // the cache next to SettingsPage's pre-restore `localSettings`, which is
    // precisely the state its debounced effect writes back. So the cache is
    // pinned to the copy the page already agrees with until the exit reload.
    it('pins the settings cache against a background refetch landing under the result panel', async () => {
      const preRestore = { currency: 'EUR' };
      let client: QueryClient | null = null;
      const Probe = makeSettingsProbe((c) => {
        client = c;
      });

      server.use(
        http.post('/api/v1/github-backup/restore', () =>
          HttpResponse.json({
            success: true,
            message: 'Restored 77 item(s) from aaa1111',
            log_id: 7,
            ref: mockPreview.ref,
            results: { settings: { restored: 77, skipped: 3, failed: 0, notes: [] } },
          })
        )
      );
      render(
        <>
          <Probe />
          <GitHubRestoreModal onClose={vi.fn()} />
        </>
      );
      // The copy SettingsPage's form state was built from.
      client!.setQueryData(['settings'], preRestore);

      const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
      await userEvent.click(checkboxes[2]);
      await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));
      await waitFor(() => screen.getByText('Restore from backup?'));
      const confirmButtons = screen.getAllByRole('button', { name: /Restore$/ });
      await userEvent.click(confirmButtons[confirmButtons.length - 1]);
      await waitFor(() => screen.getByText('Restored 77 item(s) from aaa1111'));

      // A focus/reconnect refetch lands the restored server values.
      client!.setQueryData(['settings'], { currency: 'USD' });

      // Pinned back, so SettingsPage sees no divergence and saves nothing.
      expect(client!.getQueryData(['settings'])).toEqual(preRestore);
    });

    it('leaves the settings cache alone when settings were not restored', async () => {
      let client: QueryClient | null = null;
      const Probe = makeSettingsProbe((c) => {
        client = c;
      });
      server.use(
        http.post('/api/v1/github-backup/restore', () =>
          HttpResponse.json({
            success: true,
            message: 'Restored 4 item(s) from aaa1111',
            log_id: 8,
            ref: mockPreview.ref,
            results: { spools: { restored: 4, skipped: 0, failed: 0, notes: [] } },
          })
        )
      );
      render(
        <>
          <Probe />
          <GitHubRestoreModal onClose={vi.fn()} />
        </>
      );
      client!.setQueryData(['settings'], { currency: 'EUR' });

      const checkboxes = await waitFor(() => screen.getAllByRole('checkbox') as HTMLInputElement[]);
      await userEvent.click(checkboxes[1]);
      await userEvent.click(screen.getByRole('button', { name: /Restore$/ }));
      await waitFor(() => screen.getByText('Restore from backup?'));
      const confirmButtons = screen.getAllByRole('button', { name: /Restore$/ });
      await userEvent.click(confirmButtons[confirmButtons.length - 1]);
      await waitFor(() => screen.getByText('Restored 4 item(s) from aaa1111'));

      const fresh = { currency: 'USD' };
      client!.setQueryData(['settings'], fresh);
      // No settings were touched, so there is nothing to protect and normal
      // refetching must keep working.
      expect(client!.getQueryData(['settings'])).toEqual(fresh);
    });

    it('closes normally when settings were not part of the restore', async () => {
      const loc = stubReload();
      try {
        const onClose = await restoreWith({
          spools: { restored: 4, skipped: 0, failed: 0, notes: [] },
        });

        const closeButtons = screen.getAllByRole('button', { name: 'Close' });
        await userEvent.click(closeButtons[closeButtons.length - 1]);

        expect(onClose).toHaveBeenCalled();
        expect(loc.reload).not.toHaveBeenCalled();
      } finally {
        loc.restore();
      }
    });
  });
});
