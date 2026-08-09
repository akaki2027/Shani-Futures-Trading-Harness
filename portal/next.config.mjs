/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The portal is a pure client of the Shani API. Proxying in development keeps
  // the browser on one origin, so there is no CORS preflight and the bearer
  // token never has to be handled by two different origins.
  async rewrites() {
    const api = process.env.SHANI_API_URL ?? 'http://127.0.0.1:8420';
    return [
      { source: '/api/shani/:path*', destination: `${api}/api/:path*` },
    ];
  },
};

export default nextConfig;
