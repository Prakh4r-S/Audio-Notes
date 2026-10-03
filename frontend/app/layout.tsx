import type { Metadata } from "next";
import "./globals.css";
import { Nav } from "@/components/Nav";
import { ServerStatus } from "@/components/ServerStatus";

export const metadata: Metadata = {
  title: "Audio Notes",
  description: "Upload a recording; get a transcript from Gnani ASR and an LLM summary.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Literata:opsz,wght@7..72,400;7..72,500&family=Schibsted+Grotesk:wght@400;500;600&display=swap"
        />
      </head>
      <body>
        <div className="shell">
          <Nav />
          <ServerStatus />
          <main>{children}</main>
        </div>
      </body>
    </html>
  );
}
