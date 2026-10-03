/** @type {import('next').NextConfig} */
const backend = (process.env.BACKEND_URL || "http://localhost:8000").replace(/\/$/, "");

const nextConfig = {
  // The browser only ever talks to this origin; Next proxies /api/* to FastAPI.
  // This avoids CORS entirely. Large audio files bypass this proxy: they are
  // PUT straight to the storage bucket using a signed URL.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
