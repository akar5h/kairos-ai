import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import { SearchBar } from "@/components/SearchBar";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Kairos",
  description: "Agent observability console",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full`}
    >
      <body className="h-full flex flex-col">
        {/* Top navigation bar — sticky, hairline border */}
        <header
          className="border-b shrink-0 flex items-center gap-4 px-4 h-10"
          style={{
            background: "var(--bg-surface)",
            borderColor: "var(--bg-border)",
            position: "sticky",
            top: 0,
            zIndex: 40,
          }}
        >
          {/* Wordmark */}
          <Link
            href="/"
            className="flex items-center gap-1.5 font-semibold text-xs tracking-tight shrink-0"
            style={{ color: "var(--text-primary)" }}
          >
            <span
              className="font-mono px-1 py-0.5 rounded text-[10px] font-bold tracking-widest"
              style={{
                background: "var(--accent-blue-dim)",
                color: "var(--accent-blue)",
                border: "1px solid rgba(37,99,235,0.2)",
              }}
            >
              K
            </span>
            <span>Kairos</span>
          </Link>

          {/* Nav links */}
          <nav className="flex items-center gap-3 text-xs" style={{ color: "var(--text-muted)" }}>
            <Link
              href="/"
              className="hover:text-[var(--text-primary)] transition-colors"
            >
              sessions
            </Link>
            <Link
              href="/dashboard"
              className="hover:text-[var(--text-primary)] transition-colors"
            >
              dashboard
            </Link>
            <Link
              href="/clusters"
              className="hover:text-[var(--text-primary)] transition-colors"
            >
              clusters
            </Link>
          </nav>

          {/* Global search — takes remaining space */}
          <div className="flex-1 max-w-lg">
            <SearchBar />
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 flex flex-col overflow-hidden">
          {children}
        </main>
      </body>
    </html>
  );
}
