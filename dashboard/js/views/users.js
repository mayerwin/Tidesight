// Users (admins only): who can sign in and their role, inviting people, password links for those who forgot theirs,
// removing people. Tidesight sends no email: an invitation or a password link is a one-time link the admin copies
// and passes on (chat, email); whoever opens it chooses a password. Users of the configuration file are listed only.
import { send } from '../api.js';
import { card, el, icon, table } from '../charts.js';
import { hint } from '../dom.js';
import * as fmt from '../fmt.js';
import { confirm, copyText, dialog, field, menu, segmented, toast } from '../ui.js';

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$/;

// The link a person opens: this dashboard's address, the token after a # (browsers never send that part).
export function passwordLink(token) {
  return location.origin + location.pathname + '#password=' + token;
}

export async function render(root, ctx) {
  const res = await ctx.rawApi('users');
  const refresh = () => { root.replaceChildren(); return render(root, ctx); };
  const head = card({ title: 'Users', span: 12, className: 'props-head' });
  const invite = el('button', { type: 'button', class: 'btn primary' }, icon('plus'), el('span', { text: 'Invite someone' }));
  invite.addEventListener('click', () => inviteDialog(refresh));
  head.body.appendChild(el('div', { class: 'props-bar' },
    el('p', { class: 'note', text: fmt.plural(res.users.length, 'user') + '. Admins manage properties and users; viewers read every report. ' +
      'Tidesight sends no email: an invitation is a link you pass on, and the person chooses their own password.' }),
    el('span', { class: 'spacer' }), invite));
  root.appendChild(head.root);

  const list = card({ title: 'People', span: 12 });
  table(list.body, [
    { key: 'name', label: 'User', render: (u) => el('span', { class: 'user-cell' }, icon('user'), el('b', { text: u.name }),
      u.you ? el('span', { class: 'badge accent', text: 'You' }) : null,
      u.source === 'config' ? hint(el('span', { class: 'badge', text: 'Config file' }), 'Defined in tidesight.toml ([dashboard] users): change it there') : null),
    exportValue: (u) => u.name },
    { key: 'role', label: 'Role', render: (u) => roleCell(u, refresh), exportValue: (u) => u.role },
    { key: 'status', label: 'Status', render: (u) => statusCell(u), exportValue: (u) => u.status },
    { key: 'last_sign_in', label: 'Last sign-in', format: (v) => (v ? fmt.ago(v) : 'never') },
    { key: 'actions', label: '', sortable: false, export: false, render: (u) => actionsCell(u, refresh) },
  ], res.users, { sortKey: null, searchable: res.users.length > 12, empty: 'No user yet.' });
  root.appendChild(list.root);
  root.appendChild(el('p', { class: 'note span-12', text: 'Forgot a password? Make a password link for that person here. ' +
    'If no admin can sign in any more, run "python3 -m tidesight.jobs user link NAME" on the server: it prints one.' }));
}

function roleCell(u, refresh) {
  const label = u.role === 'admin' ? 'Admin' : 'Viewer';
  if (u.source === 'config' || u.you || u.role_locked) {
    const span = el('span', { text: label });
    hint(span, u.you ? 'Another admin can change your role' : 'Set in tidesight.toml ([dashboard] admins)');
    return span;
  }
  const sel = el('select', { class: 'input small', 'aria-label': 'Role of ' + u.name },
    el('option', { value: 'admin', text: 'Admin', selected: u.role === 'admin' || null }),
    el('option', { value: 'viewer', text: 'Viewer', selected: u.role === 'viewer' || null }));
  sel.addEventListener('change', async () => {
    try {
      await send('PATCH', 'user', { role: sel.value }, { name: u.name });
      toast(u.name + ' is now ' + (sel.value === 'admin' ? 'an admin' : 'a viewer'), { kind: 'success' });
      refresh();
    } catch (e) {
      sel.value = u.role;
      toast(e.message, { kind: 'error' });
    }
  });
  return sel;
}

function statusCell(u) {
  if (u.status === 'active') return el('span', { class: 'status-pill ok' }, el('i'), el('span', { text: 'Active' }));
  if (u.status === 'invited') {
    const pill = el('span', { class: 'status-pill idle' }, el('i'), el('span', { text: 'Invited' }));
    hint(pill, 'The invitation works until ' + fmt.when(u.link.expires_at));
    return pill;
  }
  return el('span', { class: 'status-pill warn' }, el('i'), el('span', { text: 'Invitation expired' }));
}

function actionsCell(u, refresh) {
  if (u.source === 'config') return el('span');
  const more = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Actions for ' + u.name }, icon('dots'));
  more.addEventListener('click', () => menu(more, [
    { label: u.status === 'active' ? 'Password link…' : 'New invitation link…', icon: 'link', onClick: () => makeLink(u, refresh) },
    u.you ? null : 'sep',
    u.you ? null : { label: 'Remove…', icon: 'trash', danger: true, onClick: () => remove(u, refresh) },
  ], { align: 'end' }));
  return more;
}

function linkPanel(name, data) {
  const url = passwordLink(data.token);
  const input = el('input', { class: 'input wide mono', readonly: true, value: url, 'aria-label': 'Link for ' + name });
  input.addEventListener('focus', () => input.select());
  const copy = el('button', { type: 'button', class: 'btn primary' }, icon('copy'), el('span', { text: 'Copy' }));
  copy.addEventListener('click', () => copyText(url, 'Link copied: send it to ' + name));
  const until = fmt.when(data.expires_at);
  const text = data.kind === 'invite'
    ? 'Send this link to ' + name + ': it lets them choose their password and sign in, once, until ' + until + '.'
    : name + ' chooses a new password with this link, once, until ' + until + '. Their current password works until then; using the link signs them out everywhere else.';
  return el('div', { class: 'stack link-panel' }, el('p', { text: text }), el('div', { class: 'copy-row' }, input, copy),
    el('p', { class: 'note', text: 'Anyone with the link can use it: send it privately. A newer link replaces this one.' }));
}

function inviteDialog(refresh) {
  const name = el('input', { class: 'input wide', name: 'invite-name', autocomplete: 'off', spellcheck: 'false', placeholder: 'e.g. alice or alice@example.com' });
  let role = 'viewer';
  const roles = segmented([{ id: 'viewer', label: 'Viewer' }, { id: 'admin', label: 'Admin' }], role, (v) => { role = v; }, { label: 'Role' });
  const err = el('div', { class: 'login-error', role: 'alert', hidden: true });
  const body = el('div', { class: 'stack' },
    field('User name', name, 'What they type to sign in: letters, digits and . _ @ + -'),
    field('Role', roles, 'Viewers read every report and save segments. Admins also manage properties and users.'), err);
  const cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
  const create = el('button', { type: 'button', class: 'btn primary', text: 'Create the invitation' });
  const d = dialog({ title: 'Invite someone', body, actions: [cancel, create], width: 520 });
  cancel.addEventListener('click', () => d.close());
  const go = async () => {
    err.hidden = true;
    if (!NAME_RE.test(name.value)) { err.textContent = 'Choose a user name: letters, digits and . _ @ + - (starting with a letter or digit).'; err.hidden = false; name.focus(); return; }
    create.disabled = true;
    try {
      const data = await send('POST', 'users', { name: name.value, role });
      d.el.querySelector('h2').textContent = 'Invitation for ' + data.name;
      d.body.replaceChildren(linkPanel(data.name, data));
      d.foot.replaceChildren(el('button', { type: 'button', class: 'btn', text: 'Done', onclick: () => d.close() }));
      refresh();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
      create.disabled = false;
    }
  };
  create.addEventListener('click', go);
  name.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
}

async function makeLink(u, refresh) {
  try {
    const data = await send('POST', 'user_link', {}, { name: u.name });
    const d = dialog({ title: (data.kind === 'invite' ? 'Invitation link for ' : 'Password link for ') + u.name, body: linkPanel(u.name, data),
      actions: [el('button', { type: 'button', class: 'btn', text: 'Done', onclick: () => d.close() })], width: 560 });
    refresh();
  } catch (e) {
    toast(e.message, { kind: 'error' });
  }
}

async function remove(u, refresh) {
  const ok = await confirm({ title: 'Remove ' + u.name + '?', danger: true, confirmLabel: 'Remove',
    message: u.name + ' will not be able to sign in any more, and is signed out now. Their saved segments and notes stay.' });
  if (!ok) return;
  try {
    await send('DELETE', 'user', undefined, { name: u.name });
    toast(u.name + ' was removed', { kind: 'success' });
    refresh();
  } catch (e) {
    toast(e.message, { kind: 'error' });
  }
}
