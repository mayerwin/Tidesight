// Playwright runs the fixture site (port 8788) and the Python collector (port 8787) on different
// origins, as in production. Tests read what the collector stored through /__test/* endpoints.
import { defineConfig } from '@playwright/test';

const python = process.env.TIDESIGHT_PYTHON || (process.platform === 'win32' ? 'py -3' : 'python3');

export default defineConfig({
  testDir: './test',
  timeout: 30000,
  expect: { timeout: 10000 },
  fullyParallel: true,
  workers: process.env.CI ? 2 : 4,
  reporter: [['list']],
  globalSetup: './test/global-setup.mjs',
  use: { baseURL: 'http://127.0.0.1:8788', browserName: 'chromium', trace: 'retain-on-failure' },
  webServer: {
    command: `${python} ../server/python/devserver.py --config test/tidesight.test.toml --port 8787 ` +
      '--static test/fixtures --static-port 8788 --test-api',
    url: 'http://127.0.0.1:8787/health',
    reuseExistingServer: !process.env.CI,
    timeout: 30000,
    stdout: 'ignore',
    stderr: 'pipe'
  }
});
