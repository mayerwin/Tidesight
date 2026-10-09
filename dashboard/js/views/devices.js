// Technology: device types, browsers and systems (with versions), screens, brands and models.
import { reportPage } from './report-page.js';

export function render(root, ctx) {
  reportPage(root, ctx, { title: 'Technology', tabs: [
    { dim: 'device', label: 'Devices' }, { dim: 'browser', label: 'Browsers' }, { dim: 'browser_version', label: 'Browser versions' },
    { dim: 'os', label: 'Systems' }, { dim: 'os_version', label: 'System versions' }, { dim: 'screen', label: 'Screens' },
    { dim: 'brand', label: 'Brands' }, { dim: 'model', label: 'Models' }] });
}
