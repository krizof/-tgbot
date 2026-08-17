import type { Metadata } from "next";
import Script from "next/script";
import "./globals.css";

export const metadata: Metadata = {
  title: "Friends Arcade",
  description: "Мини-игры и общий рейтинг компании друзей",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ru"><body>{children}</body><Script src="https://telegram.org/js/telegram-web-app.js" strategy="beforeInteractive" /></html>;
}
