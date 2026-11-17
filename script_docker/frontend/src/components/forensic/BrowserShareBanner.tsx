import { useEffect, useState } from "react";
import { Laptop } from "lucide-react";

import { formatClientBytes } from "../../lib/clientEvidenceAgent";
import { activeBrowserShares, subscribeBrowserShares, type BrowserShareStats } from "../../lib/browserEvidenceShare";

/**
 * Shown on every page while this tab serves evidence folders chosen in the browser: closing or
 * reloading the tab pauses processing on the server, so the examiner must see it is in use.
 */
export function BrowserShareBanner() {
  const [shares, setShares] = useState<BrowserShareStats[]>(() => activeBrowserShares());

  useEffect(() => {
    const read = () => setShares(activeBrowserShares());
    const unsubscribe = subscribeBrowserShares(() => {
      // Starting and stopping are shown at once; byte counters follow the 1.5 s refresh below.
      const next = activeBrowserShares();
      setShares((prev) => (prev.length !== next.length ? next : prev));
    });
    const id = window.setInterval(read, 1500);
    return () => {
      unsubscribe();
      window.clearInterval(id);
    };
  }, []);

  if (!shares.length) return null;
  const read = shares.reduce((sum, s) => sum + s.logicalBytes, 0);
  const waiting = shares.some((s) => s.state === "waiting" || s.state === "connecting");
  const names = shares.map((s) => s.name).join(", ");
  return (
    <div
      className="sticky top-0 z-40 flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-amber-300 bg-amber-50 px-4 py-2 text-xs text-amber-950"
      role="status"
      data-testid="browser-share-banner"
    >
      <Laptop className="h-4 w-4 shrink-0 text-amber-700" />
      <span className="font-semibold">Serving evidence from this computer:</span>
      <span className="min-w-0 truncate">{names}</span>
      <span className="text-amber-800">
        · {formatClientBytes(read)} read by the forensic server so far
        {waiting ? " · connecting to the server…" : ""} · keep this tab open until processing finishes.
      </span>
    </div>
  );
}
