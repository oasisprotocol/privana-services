import type { Metadata } from "next";
import { Geist } from "next/font/google";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import "./globals.css";
import { Providers } from "./providers";

const geist = Geist({ subsets: ["latin"], variable: "--font-sans" });

export const metadata: Metadata = {
  title: "FlexVaults Earn Tester",
  description: "Local-only test bench for the FlexVaults earn pipeline"
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={cn("dark h-full antialiased", geist.variable)} suppressHydrationWarning>
      <body className="min-h-full font-sans">
        <Providers>
          <TooltipProvider delay={150}>
            {children}
            <Toaster richColors closeButton />
          </TooltipProvider>
        </Providers>
      </body>
    </html>
  );
}
