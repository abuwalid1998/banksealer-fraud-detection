import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { api, getToken, setToken } from "./api";

export type User = { id: number; username: string; full_name?: string; role: "admin" | "analyst" | "viewer" };
type AuthState = {
  user: User | null; loading: boolean;
  login: (u: string, p: string) => Promise<void>; logout: () => void; can: (role: string) => boolean;
};
const RANK: Record<string, number> = { viewer: 0, analyst: 1, admin: 2 };
const Ctx = createContext<AuthState>(null as unknown as AuthState);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const onLogout = () => setUser(null);
    window.addEventListener("banksealer:logout", onLogout);
    if (getToken()) api.get<User>("/auth/me").then(setUser).catch(() => setUser(null)).finally(() => setLoading(false));
    else setLoading(false);
    return () => window.removeEventListener("banksealer:logout", onLogout);
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const res = await api.post("/auth/login", { username, password });
    setToken(res.access_token);
    setUser(res.user);
  }, []);
  const logout = useCallback(() => { setToken(null); setUser(null); }, []);
  const can = useCallback((role: string) => !!user && RANK[user.role] >= RANK[role], [user]);
  return <Ctx.Provider value={{ user, loading, login, logout, can }}>{children}</Ctx.Provider>;
}

export const useAuth = () => useContext(Ctx);
