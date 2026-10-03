"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export function Nav() {
  const path = usePathname();
  const onRecordings = path === "/" || path.startsWith("/recordings");
  return (
    <header className="topbar">
      <Link href="/" className="brand" aria-label="Audio Notes home">
        <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
          {[3, 7, 11, 15, 19].map((x, i) => (
            <rect key={x} x={x - 1} y={11 - [3, 7, 9, 5, 2][i]} width="2" height={[3, 7, 9, 5, 2][i] * 2} rx="1" fill="currentColor" />
          ))}
        </svg>
        Audio Notes
      </Link>
      <nav className="nav">
        <Link href="/" aria-current={onRecordings ? "page" : undefined}>Recordings</Link>
        <Link href="/architecture" aria-current={path === "/architecture" ? "page" : undefined}>Architecture</Link>
      </nav>
    </header>
  );
}
