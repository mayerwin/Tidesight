// Locations: a choropleth of visitors by country with city dots, then the countries, regions, cities, languages,
// time zones and networks report.
import { card, worldMap } from '../charts.js';
import * as fmt from '../fmt.js';
import { reportPage } from './report-page.js';

export async function render(root, ctx) {
  const map = card({ title: 'Visitors by country', span: 12, info: 'Click a country to filter every report by it.' });
  root.appendChild(map.root);
  const geo = await ctx.api('geo', {}, { compare: false });
  const values = {};
  for (const r of geo.countries) if (r.value && r.value !== '(none)') values[r.value] = r.visitors;
  const points = geo.points.filter((p) => p.lat !== null && p.lon !== null)
    .map((p) => ({ lat: p.lat, lon: p.lon, value: p.visitors, label: (p.city || 'Unknown city') + ', ' + fmt.country(p.country) }));
  await worldMap(map.body, { values, points, label: 'Visitors', onClick: (cc) => ctx.setFilter('country', cc) });
  reportPage(root, ctx, { title: 'Locations', chart: false, tabs: [
    { dim: 'country', label: 'Countries' }, { dim: 'region', label: 'Regions' }, { dim: 'city', label: 'Cities' },
    { dim: 'language', label: 'Languages' }, { dim: 'timezone', label: 'Time zones' }, { dim: 'asn', label: 'Networks' }] });
}
