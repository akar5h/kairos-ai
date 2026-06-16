import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
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
  description: "Agent trace debugger",
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
        {/* Top navigation bar */}
        <header
          className="border-b flex items-center gap-6 px-6 py-3 shrink-0"
          style={{
            background: "var(--bg-surface)",
            borderColor: "var(--bg-border)",
          }}
        >
          <Link
            href="/"
            className="flex items-center gap-2 font-semibold tracking-tight text-sm"
            style={{ color: "var(--text-primary)" }}
          >
            {/* Minimal wordmark — no logo image needed */}
            <span
              className="font-mono text-xs px-1.5 py-0.5 rounded"
              style={{
                background: "var(--accent-blue-dim)",
                color: "var(--accent-blue)",
                letterSpacing: "0.05em",
              }}
            >
              K
            </span>
            <span>Kairos</span>
          </Link>
          <nav className="flex items-center gap-4 text-sm" style={{ color: "var(--text-secondary)" }}>
            <Link
              href="/"
              className="hover:text-[var(--text-primary)] transition-colors"
            >
              Traces
            </Link>
          </nav>
        </header>

        {/* Page content */}
        <main className="flex-1 flex flex-col overflow-hidden">
          {children}
        </main>
      </body>
    </html>
  );
}
