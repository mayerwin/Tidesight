// The dashboard in a browser, on demo data: periods and comparisons, filters and segments, report tables and
// exports, goals, properties, the command palette, signing in, the first account, people, updates, and the public page. No test may
// leave an error in the page.
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, test as base } from '@playwright/test';

const OPEN = 'http://127.0.0.1:8795/dashboard/';
const AUTH = 'http://127.0.0.1:8796/dashboard/';
const FRESH = 'http://127.0.0.1:8797/dashboard/'; // no user yet (test/dashboard/serve.py)
const SETUP_CODE = fileURLToPath(new URL('./.data/setup/data/setup-code.txt', import.meta.url));
const DATA_DIR = fileURLToPath(new URL('./.data/data/', import.meta.url)); // the data of 8795 and 8796

const test = base.extend({
  errors: [async ({ page }, use) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
    page.on('console', (m) => { if (m.type() === 'error' && !/status of 401/.test(m.text())) errors.push(m.text()); });
    await use(errors);
    expect(errors, 'errors in the page').toEqual([]);
  }, { auto: true }],
});

async function open(page, hash = '#/overview') {
  await page.goto(OPEN + hash);
  await expect(page.locator('#view .grid')).toBeVisible();
  await page.waitForLoadState('networkidle');
}
const hashParams = (page) => new URLSearchParams(new URL(page.url()).hash.split('?')[1] || '');

test('the overview shows KPIs, the trend and breakdowns', async ({ page }) => {
  await open(page);
  await expect(page.locator('.tile')).toHaveCount(6);
  await expect(page.locator('.overview-main path.series')).toHaveCount(1);
  await expect(page.locator('.card-title', { hasText: 'Acquisition' })).toBeVisible();
  await expect(page.locator('.bar-row').first()).toBeVisible();
  // A tile charts its metric.
  await page.locator('.tile', { hasText: 'Pageviews' }).click();
  await expect.poll(() => hashParams(page).get('metric')).toBe('pageviews');
});

test('periods: presets, the calendar, previous and next, keyboard', async ({ page }) => {
  await open(page);
  await page.locator('.date-btn').click();
  await page.locator('.dp-preset', { hasText: 'Last 7 days' }).click();
  await expect.poll(() => hashParams(page).get('period')).toBe('7d');
  await expect(page.locator('.date-text b')).toHaveText('Last 7 days');
  await page.locator('button[aria-label="Previous period"]').click();
  await expect.poll(() => hashParams(page).get('period')).toBe('custom');
  const from = hashParams(page).get('from');
  await page.locator('body').press('m');
  await expect.poll(() => hashParams(page).get('period')).toBe('30d');
  // A range from the calendar.
  await page.locator('.date-btn').click();
  const days = page.locator('.dp-day:not([disabled])');
  await days.nth(3).click();
  await days.nth(9).click();
  await page.locator('.dp-foot .btn.primary').click();
  await expect.poll(() => hashParams(page).get('period')).toBe('custom');
  expect(hashParams(page).get('from')).not.toBe(from);
});

test('comparing draws the other period dashed and shows changes', async ({ page }) => {
  await open(page, '#/overview?period=7d'); // the demo data covers three weeks
  await page.getByRole('button', { name: 'Compare', exact: true }).click();
  await page.getByRole('menuitem', { name: /Previous period/ }).click();
  await expect(page.locator('.cmp-pill')).toBeVisible();
  await expect(page.locator('.overview-main path.series.dashed')).toHaveCount(1);
  await expect(page.locator('.tile .delta').first()).toBeVisible();
  await expect(page.locator('.bar-row .delta').first()).toBeVisible();
  await page.locator('.cmp-pill button').click();
  await expect(page.locator('.cmp-pill')).toHaveCount(0);
});

test('filters: the builder, row clicks, exclusions, removing', async ({ page }) => {
  await open(page);
  const visitors = async () => (await page.locator('.tile', { hasText: 'Visitors' }).locator('.tile-value span').first().textContent()).trim();
  const before = await visitors();
  await page.locator('[data-add-filter]').click();
  await page.locator('.fb input[type=search]').fill('coun');
  await page.locator('.fb-dim', { hasText: 'Country' }).click();
  await page.locator('.fb-option').first().click();
  await page.locator('.fb-foot .btn.primary').click();
  await expect(page.locator('.chip')).toHaveCount(1);
  await expect(page.locator('.chip')).toContainText('Country is');
  expect(JSON.parse(hashParams(page).get('filters'))[0][0]).toBe('country');
  await expect.poll(visitors).not.toBe(before);
  await page.locator('.chip-x').click();
  await expect(page.locator('.chip')).toHaveCount(0);
  // A click on a row filters by it; the row's minus button excludes it.
  await page.locator('.card', { hasText: 'Acquisition' }).locator('.bar-row').first().click();
  await expect(page.locator('.chip')).toContainText('Channel is');
  await page.locator('.chip-x').click();
  const row = page.locator('.card', { hasText: 'Acquisition' }).locator('.bar-row').first();
  await row.hover();
  await row.locator('.row-actions button').click();
  await expect(page.locator('.chip.negative')).toContainText('Channel is not');
});

test('report tables: sort, search, paging, a second dimension, exports', async ({ page }) => {
  await open(page, '#/sources');
  const table = page.locator('table.report');
  await expect(table.locator('tbody tr').nth(1)).toBeVisible();
  await table.locator('th', { hasText: 'Visits' }).click();
  await expect.poll(() => hashParams(page).get('sort')).toBe('sessions');
  await page.locator('.rt-tools input[type=search]').fill('organic');
  await expect(table.locator('tbody tr:not(.totals)').first()).toContainText('Organic');
  await expect.poll(() => hashParams(page).get('q')).toBe('organic');
  await page.locator('.rt-tools input[type=search]').fill('');
  await page.getByRole('button', { name: 'Secondary dimension' }).click();
  await page.locator('.fb-dim', { hasText: /^Device$/ }).click();
  await expect(table.locator('th', { hasText: 'Device' })).toBeVisible();
  await expect.poll(() => hashParams(page).get('dim2')).toBe('device');
  // Ticking rows plots them.
  await page.getByRole('button', { name: 'Device', exact: true }).click();
  await page.locator('.fb-dim', { hasText: 'None (remove)' }).click();
  await table.locator('tbody tr:not(.totals) input[type=checkbox]').nth(1).check();
  await expect(page.locator('.chart-note')).toContainText('ticked');
  // Every row as a file: CSV and Excel.
  const [csv] = await Promise.all([page.waitForEvent('download'), (async () => {
    await page.locator('.rt-tools').getByRole('button', { name: 'Export' }).click();
    await page.getByRole('menuitem', { name: /^CSV/ }).click();
  })()]);
  expect(csv.suggestedFilename()).toMatch(/^demo_channel_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.csv$/);
  const [xlsx] = await Promise.all([page.waitForEvent('download'), (async () => {
    await page.locator('.rt-tools').getByRole('button', { name: 'Export' }).click();
    await page.getByRole('menuitem', { name: /^Excel/ }).click();
  })()]);
  expect(xlsx.suggestedFilename()).toMatch(/\.xlsx$/);
  // Tabs switch the dimension.
  await page.locator('.report-card .tabs button', { hasText: 'Campaigns' }).click();
  await expect.poll(() => hashParams(page).get('dim')).toBe('campaign');
});

test('goals: conversions, a goal in every report', async ({ page }) => {
  await open(page, '#/goals');
  const goals = page.locator('.card', { hasText: 'Goals' }).first().locator('tbody tr');
  await expect(goals).toHaveCount(3);
  await goals.filter({ hasText: 'Purchase' }).click();
  await expect.poll(() => hashParams(page).get('goal')).toBe('purchase');
  await expect(page.locator('.chip.goal')).toContainText('Purchase');
  await expect(page.locator('table.report th', { hasText: 'Conv.' }).first()).toBeVisible();
  await page.locator('#nav a', { hasText: 'Acquisition' }).click();
  await expect(page.locator('table.report th', { hasText: 'Conv. rate' })).toBeVisible();
  await page.locator('.chip.goal .chip-x').click();
  await expect(page.locator('.chip.goal')).toHaveCount(0);
});

test('the export dialog says what each download holds', async ({ page }) => {
  await open(page, '#/sources');
  await page.locator('body').press('e');
  const dialog = page.locator('.export-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.export-row', { hasText: 'Acquisition: Channel' })).toBeVisible();
  const visits = dialog.locator('.export-row', { hasText: /^Visits/ });
  await expect(visits).toContainText(/\d rows/);
  const [file] = await Promise.all([page.waitForEvent('download'), visits.getByRole('button', { name: 'Download' }).click()]);
  expect(file.suggestedFilename()).toMatch(/^tidesight-demo-sessions-.*\.csv$/);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
});

test('properties: add one, change its settings and goals, delete it', async ({ page }) => {
  // A fresh name each run: a deleted property's id stays taken until the next cron run deletes its data.
  const n = String(Date.now() % 1000000);
  const id = 'shop' + n + '-example-org';
  await open(page, '#/properties');
  await page.getByRole('button', { name: 'Add property' }).click();
  await page.locator('.wizard input').first().fill('Shop' + n + '.Example.org');
  await page.locator('.dialog-foot .btn.primary').click();
  await page.locator('.preset', { hasText: 'Privacy-first' }).click();
  await page.locator('.dialog-foot .btn.primary').click();
  await expect(page.locator('.snippet pre').first()).toContainText('data-site="' + id + '"');
  await expect(page.locator('.wait-box')).toContainText('Waiting for the first visit');
  await page.locator('.dialog-foot .btn.primary').click(); // open its reports
  await expect.poll(() => hashParams(page).get('site')).toBe(id);
  await expect(page.locator('.site-btn')).toContainText('shop' + n + '.example.org');
  // Settings: a name, a goal.
  await page.goto(OPEN + '#/property?site=' + id);
  await page.locator('.field', { hasText: 'Name' }).locator('input').fill('The Shop');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.locator('.toast', { hasText: /^Saved$/ })).toBeVisible();
  await expect(page.locator('.site-btn')).toContainText('The Shop');
  await page.locator('.tabs a', { hasText: 'Goals' }).click();
  await page.locator('.goal-add input[placeholder="e.g. Signup"]').fill('Checkout');
  await page.locator('.goal-add input[list="goal-suggest"]').fill('checkout');
  await page.getByRole('button', { name: 'Add goal' }).click();
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.locator('.toast', { hasText: 'Goals saved' })).toBeVisible();
  await expect(page.locator('table.data tbody')).toContainText('Checkout');
  // A domain that is not one is refused.
  await page.locator('.tabs a', { hasText: 'General' }).click();
  await page.locator('.tag-entry').fill('not a domain');
  await page.locator('.tag-entry').press('Enter');
  await expect(page.locator('.field-error')).toContainText('not a domain');
  // Delete, keeping nothing.
  await page.locator('.tabs a', { hasText: 'Delete' }).click();
  await page.getByRole('button', { name: 'Delete property…' }).click();
  await page.locator('.dialog .segmented button', { hasText: 'Delete its data' }).click();
  await page.locator('.dialog input.mono').fill(id);
  await page.locator('.dialog .btn.danger').click();
  await expect.poll(() => new URL(page.url()).hash).toMatch(/^#\/properties/);
  await expect(page.locator('.prop-card')).toHaveCount(1);
  await expect(page.locator('.notice.warn', { hasText: id })).toBeVisible();
});

test('segments: save the filters, apply them again', async ({ page }) => {
  const n = String(Date.now() % 1000000); // segments outlive a run: a fresh name and filters each time
  const name = 'Mobile visits ' + n;
  const filters = [['device', 'is', ['mobile']], ['page', 'not_contains', ['/never-' + n]]];
  await open(page, '#/overview?filters=' + encodeURIComponent(JSON.stringify(filters)));
  await page.getByRole('button', { name: 'Segments' }).click();
  await page.getByRole('menuitem', { name: /Save current filters/ }).click();
  await page.locator('.dialog input').first().fill(name);
  await page.locator('.dialog .btn.primary').click();
  await expect(page.locator('.toast', { hasText: 'Segment saved' })).toBeVisible();
  await expect(page.locator('.bar-right .ctl.active')).toContainText(name);
  await page.locator('.chips .link-btn', { hasText: 'Clear all' }).click();
  await expect(page.locator('.chip')).toHaveCount(0);
  await page.getByRole('button', { name: 'Segments' }).click();
  await page.getByRole('menuitemradio', { name: new RegExp(name) }).click();
  await expect(page.locator('.chip').first()).toContainText('Device is');
  await expect(page.locator('.chip')).toHaveCount(2);
  // Clean up through the API (a change needs the X-Tidesight header).
  const list = await (await page.request.get(OPEN + 'api/segments?site=demo')).json();
  const mine = list.segments.find((x) => x.name === name);
  expect((await page.request.delete(OPEN + 'api/segment?id=' + mine.id, { headers: { 'X-Tidesight': '1' } })).status()).toBe(200);
});

test('the command palette goes anywhere', async ({ page }) => {
  await open(page);
  await page.keyboard.press('Control+k');
  await expect(page.locator('.palette-input')).toBeFocused();
  await page.keyboard.type('Browser version');
  await page.keyboard.press('Enter');
  await expect.poll(() => new URL(page.url()).hash).toMatch(/^#\/report\?.*dim=browser_version/);
  await expect(page.locator('table.report th', { hasText: 'Browser version' })).toBeVisible();
});

test('signing in and out; viewers cannot change properties', async ({ page }) => {
  await page.goto(AUTH);
  await expect(page.locator('.login-form')).toBeVisible();
  await page.fill('input[name=username]', 'admin');
  await page.fill('input[name=password]', 'nope');
  await page.click('button[type=submit]');
  await expect(page.locator('.login-error')).toContainText('Wrong user name or password');
  await page.fill('input[name=password]', 'test-pass');
  await page.click('button[type=submit]');
  await expect(page.locator('#nav a', { hasText: 'Overview' })).toBeVisible();
  await expect(page.locator('.tile').first()).toBeVisible();
  await page.locator('.user-btn').click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await expect(page.locator('.login-form')).toBeVisible();
  await page.fill('input[name=username]', 'viewer');
  await page.fill('input[name=password]', 'view-pass');
  await page.click('button[type=submit]');
  await expect(page.locator('#nav a', { hasText: 'Overview' })).toBeVisible();
  await page.goto(AUTH + '#/properties');
  await expect(page.getByRole('button', { name: 'Add property' })).toBeDisabled();
});

test('the first account needs the setup code written on the server', async ({ page }) => {
  await page.goto(FRESH);
  await expect(page.getByRole('heading', { name: 'Welcome' })).toBeVisible();
  const fill = async (code, password, again) => {
    if (code !== null) await page.getByLabel('Setup code').fill(code);
    await page.getByLabel('User name').fill('owner');
    await page.getByLabel('Password', { exact: true }).fill(password);
    await page.getByLabel('Repeat the password').fill(again);
    await page.getByRole('button', { name: 'Create the account' }).click();
  };
  await fill('0000-0000-0000-0000', 'a long password', 'a long password');
  await expect(page.locator('.login-error')).toContainText('Wrong setup code');
  // The address `jobs.py setup-link` prints fills the code in, and leaves no trace of it in the address bar.
  const code = readFileSync(SETUP_CODE, 'utf8').trim();
  await page.goto('about:blank');
  await page.goto(FRESH + '#setup=' + code);
  await expect(page.getByLabel('Setup code')).toHaveValue(code);
  expect(new URL(page.url()).hash).toBe('');
  await fill(null, 'a long password', 'another password');
  await expect(page.locator('.login-error')).toContainText('differ');
  await fill(null, 'a long password', 'a long password');
  await expect(page.locator('.user-btn')).toBeVisible(); // signed in
  expect(existsSync(SETUP_CODE)).toBe(false); // the code is gone with its use
  await page.locator('.user-btn').click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible(); // from now on, an ordinary sign-in
  await page.fill('input[name=username]', 'owner');
  await page.fill('input[name=password]', 'a long password');
  await page.click('button[type=submit]');
  await expect(page.locator('.user-btn')).toBeVisible();
});

test('users: invite someone, a password link, a role, changing a password, removing', async ({ page, browser }) => {
  const signIn = async (p, user, password) => {
    await p.goto(AUTH);
    await p.fill('input[name=username]', user);
    await p.fill('input[name=password]', password);
    await p.click('button[type=submit]');
    await expect(p.locator('.user-btn')).toBeVisible();
  };
  // Opens a link in a browser of its own (no cookie of the admin's) and chooses the password.
  const useLink = async (url, password, heading) => {
    const context = await browser.newContext();
    const p = await context.newPage();
    await p.goto(url);
    await expect(p.getByRole('heading', { name: heading })).toBeVisible();
    expect(new URL(p.url()).hash).toBe(''); // the token is out of the address bar
    await p.getByLabel('Password', { exact: true }).fill(password);
    await p.getByLabel('Repeat the password').fill(password);
    await p.locator('button[type=submit]').click();
    await expect(p.locator('.user-btn')).toContainText('e2e-alice');
    return { context, p };
  };
  await signIn(page, 'admin', 'test-pass');
  await page.goto(AUTH + '#/users');
  await expect(page.getByRole('button', { name: 'Invite someone' })).toBeVisible(); // the users page has replaced the overview
  await expect(page.locator('#view table.data')).toContainText('admin');
  await expect(page.locator('#view table.data .badge', { hasText: 'You' })).toBeVisible();

  await page.getByRole('button', { name: 'Invite someone' }).click();
  await page.getByLabel('User name').fill('e2e-alice');
  await page.getByRole('button', { name: 'Create the invitation' }).click();
  const invite = await page.getByRole('textbox', { name: 'Link for e2e-alice' }).inputValue();
  expect(invite).toMatch(/#password=[A-Za-z0-9_-]{20,}$/);
  await page.getByRole('button', { name: 'Done' }).click();
  await expect(page.locator('tr', { hasText: 'e2e-alice' })).toContainText('Invited');

  const alice = await useLink(invite, 'alice password 1', 'Welcome, e2e-alice');
  await expect(alice.p.locator('#nav a', { hasText: 'Users' })).toHaveCount(0); // a viewer manages no one

  await page.reload();
  const row = page.locator('tr', { hasText: 'e2e-alice' });
  await expect(row).toContainText('Active');
  await row.getByRole('button', { name: 'Actions for e2e-alice' }).click();
  await page.getByRole('menuitem', { name: 'Password link…' }).click();
  const reset = await page.getByRole('textbox', { name: 'Link for e2e-alice' }).inputValue();
  await page.getByRole('button', { name: 'Done' }).click();
  const again = await useLink(reset, 'alice password 2', 'A new password');

  // Alice changes her own password from her menu.
  await again.p.locator('.user-btn').click();
  await again.p.getByRole('menuitem', { name: 'Change password…' }).click();
  await again.p.getByLabel('Current password').fill('alice password 2');
  await again.p.getByLabel('New password', { exact: true }).fill('alice password 3');
  await again.p.getByLabel('Repeat the new password').fill('alice password 3');
  await again.p.getByRole('button', { name: 'Change the password' }).click();
  await expect(again.p.locator('.toast', { hasText: 'Password changed' })).toBeVisible();

  await page.reload();
  await page.getByLabel('Role of e2e-alice').selectOption('admin');
  await expect(page.locator('.toast', { hasText: 'e2e-alice is now an admin' })).toBeVisible();
  await page.locator('tr', { hasText: 'e2e-alice' }).getByRole('button', { name: 'Actions for e2e-alice' }).click();
  await page.getByRole('menuitem', { name: 'Remove…' }).click();
  await page.getByRole('button', { name: 'Remove', exact: true }).click();
  await expect(page.locator('tr', { hasText: 'e2e-alice' })).toHaveCount(0);
  await alice.context.close();
  await again.context.close();
});

test('admins see a newer release, and its installation', async ({ page }) => {
  const repo = 'https://github.com/mayerwin/Tidesight';
  // What jobs.py cron would have found on GitHub, then an installation under way (the files of updates.py).
  writeFileSync(DATA_DIR + 'update.json', JSON.stringify({ latest: '99.0.0', release_url: repo + '/releases/tag/v99.0.0', checked_at: Date.now(), error: null }));
  try {
    await page.goto(AUTH);
    await page.fill('input[name=username]', 'admin');
    await page.fill('input[name=password]', 'test-pass');
    await page.click('button[type=submit]');
    await expect(page.locator('a.source-link')).toHaveAttribute('href', repo);
    await expect(page.locator('a.source-link svg.gh-mark')).toBeVisible();
    await page.locator('#version .update-badge').click();
    const card = page.locator('section.card', { has: page.locator('h2.card-title', { hasText: /^Updates$/ }) });
    await expect(card).toContainText('Tidesight 99.0.0 is available');
    await expect(card.getByRole('link', { name: 'What is new' })).toHaveAttribute('href', repo + '/releases/tag/v99.0.0');
    await expect(card.getByRole('button', { name: 'Update to 99.0.0' })).toBeDisabled(); // the tests run from a git checkout
    await expect(card).toContainText('git pull');
    writeFileSync(DATA_DIR + 'update-request.json', JSON.stringify({ version: '99.0.0', requested_by: 'admin', requested_at: Date.now() - 60000, started_at: Date.now() - 5000 }));
    await page.reload();
    await expect(card).toContainText('Installing Tidesight 99.0.0');
    await expect(card.getByRole('button', { name: 'Cancel' })).toHaveCount(0);
  } finally {
    rmSync(DATA_DIR + 'update.json', { force: true });
    rmSync(DATA_DIR + 'update-request.json', { force: true });
  }
});

test('the public page shows its metrics and nothing else', async ({ page }) => {
  await page.goto('http://127.0.0.1:8795/share/demo/');
  await expect(page.locator('.tile')).toHaveCount(2);
  await expect(page.locator('[data-add-filter]')).toHaveCount(0);
  await expect(page.locator('#nav a')).toHaveCount(2);
  await expect(page.locator('a.source-link')).toBeVisible();
});
