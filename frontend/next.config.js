/**
 * Two build targets share this config:
 *
 * - default (`npm run build`): a self-contained server bundle for the production Docker image.
 * - NEXT_OUTPUT=export (`npm run build:desktop`): a static export in out/ that the desktop app's
 *   Python sidecar serves on the same origin as the API. trailingSlash gives every page its own
 *   directory (out/dashboard/index.html), which any static file server can serve without rewrites.
 *
 * @type {import('next').NextConfig}
 */
const nextConfig =
  process.env.NEXT_OUTPUT === 'export'
    ? {
        reactStrictMode: true,
        output: 'export',
        trailingSlash: true,
        images: { unoptimized: true },
      }
    : {
        reactStrictMode: true,
        // Self-contained server bundle for the production Docker image.
        output: 'standalone',
      };

module.exports = nextConfig;
