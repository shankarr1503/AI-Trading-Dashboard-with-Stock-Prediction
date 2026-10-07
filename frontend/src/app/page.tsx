import { redirect } from 'next/navigation';

// Read while the page is pre-rendered, so it is fixed per build.
const STATIC_EXPORT = process.env.NEXT_OUTPUT === 'export';
const DASHBOARD = '/dashboard/';

export default function Home() {
  if (!STATIC_EXPORT) redirect('/dashboard');

  // A static export has no server to answer "/" with a redirect, so out/index.html redirects
  // itself: the inline script runs as soon as the HTML is parsed, the meta refresh covers pages
  // loaded with scripts disabled, and the link is the last resort.
  return (
    <>
      <meta httpEquiv="refresh" content={`0;url=${DASHBOARD}`} />
      <script
        dangerouslySetInnerHTML={{
          __html: `location.replace(${JSON.stringify(DASHBOARD)}+location.search+location.hash);`,
        }}
      />
      <p style={{ padding: 24, color: '#9ba3b8' }}>
        Opening the <a href={DASHBOARD} style={{ color: '#4fa3ff' }}>dashboard</a>…
      </p>
    </>
  );
}
