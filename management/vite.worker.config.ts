import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

export default defineConfig({
  resolve: {
    alias: {
      '#': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: '.worker',
    emptyOutDir: true,
    ssr: 'worker/run.ts',
    rollupOptions: {
      output: { entryFileNames: 'run.mjs' },
    },
  },
});
