// Devices: device type, browser, OS (with versions), screens, languages, brands and models.
import { breakdownCard } from '../widgets.js';

export async function render(root, ctx) {
  root.appendChild(breakdownCard(ctx, { title: 'Device types', span: 4, tabs: [{ dim: 'device', label: 'Device' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Browsers', span: 4, tabs: [
    { dim: 'browser', label: 'Browser' }, { dim: 'browser_version', label: 'Versions' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Operating systems', span: 4, tabs: [
    { dim: 'os', label: 'OS' }, { dim: 'os_version', label: 'Versions' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Screens', span: 4, tabs: [{ dim: 'screen', label: 'Screen size' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Languages', span: 4, tabs: [{ dim: 'language', label: 'Language' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Hardware', span: 4, tabs: [
    { dim: 'brand', label: 'Brand' }, { dim: 'model', label: 'Model' }] }));
}
