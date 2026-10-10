// The dashboard's browser tests: demo data (tools/demo.py) served by test/dashboard/serve.py without signing in
// (8795), with it (8796), as a new installation (8797) and as a read-only demo (8798). `npm run test:dashboard`.
import { defineConfig } from '@playwright/test';

const python = process.env.TIDESIGHT_PYTHON || (process.platform === 'win32' ? 'py -3' : 'python3');

export default defineConfig({
  testDir: './test/dashboard',
  timeout: 45000,
  expect: { timeout: 10000 },
  fullyParallel: false,
  workers: 1, // the tests change properties and segments of one shared data directory
  reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:8795', browserName: 'chromium', trace: 'retain-on-failure', viewport: { width: 1400, height: 950 } },
  webServer: {
    command: `${python} test/dashboard/serve.py`,
    url: 'http://127.0.0.1:8796/health',
    reuseExistingServer: false,
    timeout: 120000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
});
