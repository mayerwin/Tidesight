// Start every run with empty test databases (the collector keeps running between local runs).
export default async function globalSetup() {
  try {
    await fetch('http://127.0.0.1:8787/__test/reset', { method: 'POST' });
  } catch (e) {
    // The web server is started by Playwright before this runs; ignore if it is not up yet.
  }
}
