// Geography: choropleth of visitors by country with city dots, then countries, regions, cities, networks.
import { card, worldMap } from '../charts.js';
import * as fmt from '../fmt.js';
import { breakdownCard } from '../widgets.js';

export async function render(root, ctx) {
  const map = card({ title: 'Visitors by country', span: 12 });
  root.appendChild(map.root);
  const geo = await ctx.api('geo');
  const values = {};
  for (const r of geo.countries) if (r.value && r.value !== '(none)') values[r.value] = r.visitors;
  const points = geo.points.filter((p) => p.lat !== null && p.lon !== null)
    .map((p) => ({ lat: p.lat, lon: p.lon, value: p.visitors, label: (p.city || 'Unknown city') + ', ' + fmt.country(p.country) }));
  await worldMap(map.body, { values, points, label: 'Visitors', onClick: (cc) => ctx.setFilter('country', cc) });
  root.appendChild(breakdownCard(ctx, { title: 'Countries', span: 4, limit: 15, tabs: [{ dim: 'country', label: 'Country' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Regions', span: 4, limit: 15, tabs: [{ dim: 'region', label: 'Region' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Cities', span: 4, limit: 15, tabs: [{ dim: 'city', label: 'City' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Networks', span: 6, limit: 10, tabs: [{ dim: 'asn', label: 'Network (ASN)' }] }));
  root.appendChild(breakdownCard(ctx, { title: 'Time zones', span: 6, limit: 10, tabs: [{ dim: 'timezone', label: 'Time zone' }] }));
}
