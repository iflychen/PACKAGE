import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SPC 品管管制圖",
  description: "iPQC + SPC：選製程/尺寸顯示 I-MR 管制圖並標示異常點",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-Hant">
      <body>{children}</body>
    </html>
  );
}
