// Sources: channels (GA4 default channel group), source categories, sources, referrers, campaigns.
import { breakdownCard } from '../widgets.js';

export async function render(root, ctx) {
  root.appendChild(breakdownCard(ctx, { title: 'Channels', span: 6, tabs: [{ dim: 'channel', label: 'Channel' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Source categories', span: 6, tabs: [{ dim: 'category', label: 'Category' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Sources', span: 6, limit: 15, tabs: [
    { dim: 'source', label: 'Source' }, { dim: 'medium', label: 'Medium' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Referrers', span: 6, limit: 15, tabs: [{ dim: 'referrer', label: 'Referrer' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Campaigns', span: 12, limit: 15, tabs: [
    { dim: 'campaign', label: 'Campaign' }, { dim: 'term', label: 'Term' }, { dim: 'content', label: 'Content' }] }));
}
