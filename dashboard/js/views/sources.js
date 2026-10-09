// Acquisition: channels (GA4's default channel group), sources, mediums, campaigns, referrers.
import { reportPage } from './report-page.js';

export function render(root, ctx) {
  reportPage(root, ctx, { title: 'Acquisition', tabs: [
    { dim: 'channel', label: 'Channels' }, { dim: 'source', label: 'Sources' }, { dim: 'medium', label: 'Mediums' },
    { dim: 'campaign', label: 'Campaigns' }, { dim: 'term', label: 'Terms' }, { dim: 'content', label: 'Ad content' },
    { dim: 'referrer', label: 'Referrers' }, { dim: 'category', label: 'Categories' }],
  emptyHint: 'No visits in this period. Campaigns come from utm_ parameters in the landing URL.' });
}
