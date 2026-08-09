import type { Metadata } from 'next';
import { Fraunces, IBM_Plex_Sans } from 'next/font/google';
import './globals.css';

// Self-hosted at build time by next/font, so the portal has no runtime
// dependency on a font CDN. A trading tool that degrades to Times because
// Google is unreachable is not acceptable.
//
// Fraunces carries the display voice: an editorial serif with genuine
// character, chosen because Shani is named for the slow teacher and the world
// should read as patient rather than energetic. Plex Sans handles the interface
// and has the tabular figures every number in this application needs.
// Weight is deliberately omitted: Fraunces is a variable font, and omitting it
// ships the whole axis so the stylesheet can use font-weight 550 — a value no
// static cut exposes.
const fraunces = Fraunces({
  subsets: ['latin'],
  axes: ['SOFT', 'WONK', 'opsz'],
  variable: '--font-fraunces',
  display: 'swap',
});

const plex = IBM_Plex_Sans({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-plex',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'Shani',
  description: 'A trading harness that learns how you trade.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${fraunces.variable} ${plex.variable}`}>
      <body>{children}</body>
    </html>
  );
}
