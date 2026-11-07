import { useCallback, useEffect, useRef, useState } from "react";
import { describeError, get } from "../services/api";

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** GET a real endpoint. `pollMs` re-fetches on an interval (authenticated
 * polling -- the existing live-update mechanism; nothing is generated). */
export function useApi<T>(path: string | null, pollMs?: number): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(Boolean(path));
  const [tick, setTick] = useState(0);
  const first = useRef(true);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    first.current = true;
  }, [path]);

  useEffect(() => {
    if (!path) return;
    let alive = true;
    if (first.current) setLoading(true);
    get<T>(path)
      .then((d) => { if (alive) { setData(d); setError(null); } })
      .catch((e) => { if (alive) setError(describeError(e)); })
      .finally(() => { if (alive) { setLoading(false); first.current = false; } });
    return () => { alive = false; };
  }, [path, tick]);

  useEffect(() => {
    if (!path || !pollMs) return;
    const id = window.setInterval(reload, pollMs);
    return () => window.clearInterval(id);
  }, [path, pollMs, reload]);

  return { data, error, loading, reload };
}
