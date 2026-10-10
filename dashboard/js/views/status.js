// Status: updates (admins), the website folder's check, storage, enrichment progress and flagged traffic.
import { send } from '../api.js';
import { card, el, icon, table } from '../charts.js';
import * as fmt from '../fmt.js';
import { state } from '../state.js';
import { confirm, toast } from '../ui.js';

export async function render(root, ctx) {
  const st = await ctx.rawApi('status', {});
  const files = card({ title: 'Storage', span: 6 });
  table(files.body, [{ key: 'name', label: 'Database' }, { key: 'mb', label: 'Size', num: true, format: (v) => v + ' MB' }],
    Object.entries(st.files || {}).map(([name, mb]) => ({ name, mb })), { sortKey: null, empty: 'No database files yet.' });
  files.body.appendChild(el('p', { class: 'note', text: 'Storage mode: ' + st.storage_mode + ' · Tidesight ' + st.version }));
  const enrich = card({ title: 'Enrichment', span: 6 });
  const dl = el('dl', { class: 'kv' });
  const e = st.enrich || {};
  for (const [k, v] of [['Last run', e.updated_at ? fmt.when(e.updated_at) + ' (' + fmt.ago(e.updated_at) + ')' : 'never'],
    ['Last processed hit', e.last_id], ['Last hit received', st.last_hit ? fmt.when(st.last_hit) : '—'], ['Version', e.version]]) {
    dl.append(el('dt', { text: k }), el('dd', { class: k === 'Version' ? 'mono' : '', text: v === undefined || v === null ? '—' : String(v) }));
  }
  enrich.body.appendChild(dl);
  if (st.disk_free_mb !== null && st.disk_free_mb !== undefined) {
    const low = st.min_free_disk_mb > 0 && st.disk_free_mb < st.min_free_disk_mb;
    files.body.appendChild(el('p', { class: 'note', text: 'Free disk space: ' + fmt.num(st.disk_free_mb) + ' MB' +
      (st.min_free_disk_mb > 0 ? (low ? ' · below ' : ' · writes stop below ') + fmt.num(st.min_free_disk_mb) + ' MB' : '') +
      (low ? ': new data is being refused (503) until space is freed.' : '') }));
  }
  const flagged = card({ title: 'Flagged requests · last 24 hours', span: 12 });
  table(flagged.body, [{ key: 'status', label: 'Status' }, { key: 'n', label: 'Requests', num: true, format: fmt.num }],
    Object.entries(st.flagged_today || {}).map(([status, n]) => ({ status, n })), { sortKey: 'n', empty: 'Nothing flagged: every request matched a configured site and origin.' });
  flagged.body.appendChild(el('p', { class: 'note', text: 'Flagged requests (unknown site, wrong origin, malformed…) are stored but kept out of reports.' }));
  const LABELS = {
    hits: 'Hits stored', rate_limited: 'Refused: a client over its allowance', chunk_rate_limited: 'Refused: recording uploads over a client’s allowance',
    flagged_limited: 'Refused: flagged traffic over its allowance', over_quota: 'Refused: the site’s daily quota is used up',
    chunk_too_large: 'Refused: recording chunk too large', dropped_dnt: 'Not tracked: Do Not Track', dropped_gpc: 'Not tracked: Global Privacy Control'
  };
  const counters = card({ title: 'Limits and counters · today (UTC)', span: 12 });
  table(counters.body, [
    { key: 'site', label: 'Site', format: (v) => v || '(unknown or none)' },
    { key: 'name', label: 'Counter', format: (v) => LABELS[v] || v },
    { key: 'n', label: 'Count', num: true, format: fmt.num }
  ], st.counters_today || [], { sortKey: 'n', exportName: 'counters-today', empty: 'Nothing counted today.' });
  counters.body.appendChild(el('p', { class: 'note', text: 'Refused requests are counted, not stored: see "Limits and abuse protection" in docs/configuration.md.' }));
  root.append(files.root, enrich.root, flagged.root, counters.root);
  if (st.web_check) root.insertBefore(webCheckCard(st.web_check).root, files.root);
  if (state.me && state.me.admin && state.me.auth !== 'none') {
    let up = null;
    try { up = await ctx.rawApi('update'); } catch (e) { /* not available: no card */ }
    if (up) {
      const refresh = () => { root.replaceChildren(); render(root, ctx); };
      root.insertBefore(updateCard(up, refresh, () => ctx.rawApi('update')).root, root.firstChild);
    }
  }
}

// Whether a request seen pending has come to something the card should show: its installation started, it was
// installed and the server runs it, it failed, or it was withdrawn.
function settled(now, req) {
  if (!now) return false;
  if (now.request) return Boolean(now.request.started_at) !== Boolean(req.started_at);
  const last = now.last_update;
  if (last && last.to === req.version && last.at >= req.requested_at) return Boolean(last.error) || now.current === req.version;
  return true;
}

// Updates: the latest release (looked for once a day by jobs.py cron), and installing it in place. The install is
// done by the next scheduled job run, never by the web request; the previous code is kept for a rollback.
function updateCard(up, refresh, fetchState) {
  const c = card({ title: 'Updates', span: 12 });
  const say = (cls, iconName, ...parts) => c.body.appendChild(el('div', { class: 'notice' + cls }, icon(iconName), el('span', {}, ...parts)));
  const releases = up.repository ? el('a', { href: up.repository + '/releases', rel: 'noopener', text: 'the releases' }) : 'the releases';
  if (state.version && up.current !== state.version) {  // the server changed version since this page was loaded
    const reload = el('button', { type: 'button', class: 'btn primary small', text: 'Reload' });
    reload.addEventListener('click', () => location.reload());
    say('', 'check', 'The server now runs Tidesight ' + up.current + ': reload the page to use it. ', reload);
  }
  const last = up.last_update;
  if (last && last.error) say(' warn', 'alert', 'The update to ' + last.to + ' failed (' + fmt.ago(last.at) + '): ' + last.error + '. Nothing was changed.');
  else if (last && last.rollback && last.to === up.current) say('', 'info', 'Rolled back from ' + last.from + ' to ' + last.to + ' ' + fmt.ago(last.at) + '.');
  else if (last && last.to === up.current) say('', 'check', 'Updated from ' + last.from + ' to ' + last.to + ' ' + fmt.ago(last.at) + (last.requested_by ? ' (asked for by ' + last.requested_by + ')' : '') + '.');
  if (up.request) {
    const req = up.request;
    if (req.started_at) {
      say('', 'info', 'Installing Tidesight ' + req.version + ' (started ' + fmt.ago(req.started_at) + '). Recording goes on meanwhile.');
    } else {
      const cancel = el('button', { type: 'button', class: 'btn small', text: 'Cancel' });
      cancel.addEventListener('click', async () => { try { await send('DELETE', 'update'); refresh(); } catch (e) { toast(e.message, { kind: 'error' }); } });
      say('', 'info', 'Tidesight ' + req.version + ' will be installed by the next scheduled job run (within a few minutes)' +
        (req.requested_by ? ', as ' + req.requested_by + ' asked' : '') + '. ', cancel);
    }
    // Look again while it waits or installs, as long as this card is shown, and show how it went.
    let tries = 0;
    const again = () => setTimeout(async () => {
      if (!c.root.isConnected) return;
      let now = null;
      try { now = await fetchState(); } catch (e) { /* the server may be switching to the new code: look again */ }
      if (settled(now, req) || ++tries > 80) refresh(); else again();
    }, 15000);
    again();
  } else if (up.available) {
    const notes = up.release_url ? el('a', { href: up.release_url, rel: 'noopener', text: 'What is new' }) : null;
    const go = el('button', { type: 'button', class: 'btn primary small', text: 'Update to ' + up.latest, disabled: up.git || null });
    go.addEventListener('click', async () => {
      const ok = await confirm({ title: 'Update Tidesight to ' + up.latest + '?', confirmLabel: 'Update',
        message: 'The next scheduled job run (within a few minutes) downloads ' + up.latest + ' from GitHub, checks it and puts it in place of ' + up.current +
          ', which is kept for a rollback (jobs.py update --rollback). Recording goes on meanwhile; this page says when it is done.' });
      if (!ok) return;
      try { await send('POST', 'update', { version: up.latest }); refresh(); } catch (e) { toast(e.message, { kind: 'error' }); }
    });
    say(' warn', 'info', 'Tidesight ' + up.latest + ' is available: you run ' + up.current + '. ', notes, notes ? ' ' : '', go,
      up.git ? el('span', { class: 'note', text: ' This installation is a git checkout: update it with git pull.' }) : null);
  } else if (up.latest) {
    say('', 'check', 'Tidesight ' + up.current + ' is the latest release (checked ' + fmt.ago(up.checked_at) + ').');
  } else {
    say('', 'info', 'Tidesight ' + up.current + '. ' + (up.error ? up.error.charAt(0).toUpperCase() + up.error.slice(1) + '. ' : 'Not checked yet. '),
      'New versions are looked for once a day, among ', releases, '.');
  }
  return c;
}

// Tidesight's files inside the website's folder: what the last outside check found (jobs.py cron, every 10 minutes).
function webCheckCard(w) {
  const c = card({ title: 'Website folder', span: 12 });
  const say = (cls, iconName, text) => c.body.appendChild(el('div', { class: 'notice' + cls }, icon(iconName), el('span', { text })));
  if (w.exposed) {
    say(' warn', 'alert', 'Tidesight\'s files could be downloaded from ' + (w.url || 'the website') + ': it refuses to record or show anything until they cannot. Check the .htaccess of its folder (docs/deploy-dreamhost.md); the next check (within 10 minutes) clears this by itself.');
  } else if (w.unverified) {
    say(' warn', 'info', 'Not verified' + (w.verified_at ? ' since ' + fmt.when(w.verified_at) : ' yet') + ': ' + w.unverified);
  } else if (w.skipped) {
    say('', 'info', w.skipped);
  } else {
    say('', 'check', 'Checked ' + fmt.ago(w.checked_at) + ' from the outside: the files of Tidesight\'s folders cannot be downloaded.');
  }
  for (const text of w.warnings || []) say(' warn', 'alert', text);
  if (w.checked) {
    table(c.body.appendChild(el('div')), [{ key: 'what', label: 'Folder' }, { key: 'url', label: 'Asked at' },
      { key: 'status', label: 'Answer', num: true }], w.checked, { sortKey: null });
  }
  return c;
}
