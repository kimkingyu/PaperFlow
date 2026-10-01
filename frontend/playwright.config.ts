import { defineConfig } from '@playwright/test';
const realBackend = process.env.PAPERFLOW_REAL_BACKEND === '1';
export default defineConfig({
  testDir: './tests/e2e', testMatch: realBackend ? '**/real-backend.spec.ts' : '**/workbench.spec.ts', timeout: 30000, fullyParallel: false, workers: 1,
  reporter: [['list']], outputDir: realBackend ? './test-results/real' : './test-results/mock',
  use: { launchOptions: { executablePath: process.env.PAPERFLOW_TEST_BROWSER || undefined }, baseURL: realBackend ? 'http://127.0.0.1:5179' : 'http://127.0.0.1:5178', viewport: { width: 1512, height: 1000 }, screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: { command: realBackend ? 'node tests/serve-backend.mjs' : 'npm run dev -- --port 5178 --strictPort', url: realBackend ? 'http://127.0.0.1:5179/?token=isolated-real-backend-token' : 'http://127.0.0.1:5178/app/', reuseExistingServer: false },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }]
});
