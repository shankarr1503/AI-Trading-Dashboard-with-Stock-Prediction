'use client';
import { useCallback, useEffect, useState } from 'react';
import { authApi, tokens, User } from '@/lib/api';

/** Current user (null when signed out). `loading` is true until the first check completes. */
export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    if (!tokens.access) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      const r = await authApi.me();
      setUser(r.data);
    } catch {
      setUser(null);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const logout = useCallback(() => {
    authApi.logout();
    setUser(null);
  }, []);

  return { user, loading, refresh, logout };
}
