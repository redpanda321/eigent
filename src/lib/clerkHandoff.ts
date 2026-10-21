/** PKCE state for the browser-to-desktop Clerk sign-in round trip. */

const STORAGE_KEY = 'eigent-clerk-handoff';
const VALUE = /^[A-Za-z0-9_-]{22,128}$/;
const VERIFIER = /^[A-Za-z0-9_-]{43,128}$/;

export type ClerkHandoffMode = 'login' | 'link';

type Pending = {
  mode: ClerkHandoffMode;
  state: string;
  verifier: string;
  expiresAt: number;
  accountId?: number;
};

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

export function clerkHandoffAvailable(): boolean {
  try {
    const url = new URL(import.meta.env.VITE_CLERK_HANDOFF_ORIGIN || '');
    return (
      url.protocol === 'https:' &&
      !!url.hostname &&
      !url.username &&
      !url.password &&
      !url.port &&
      url.pathname === '/' &&
      !url.search &&
      !url.hash
    );
  } catch {
    return false;
  }
}

export async function startClerkHandoff(
  mode: ClerkHandoffMode,
  openExternal: (url: string) => Promise<{ success: boolean }>,
  accountId?: number | null
): Promise<void> {
  if (!clerkHandoffAvailable())
    throw new Error('Clerk sign-in is not configured');
  if (
    mode === 'link' &&
    (typeof accountId !== 'number' ||
      !Number.isSafeInteger(accountId) ||
      accountId <= 0)
  )
    throw new Error('Eigent account is required for linking');
  const origin = new URL(import.meta.env.VITE_CLERK_HANDOFF_ORIGIN).origin;
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const state = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const hash = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(verifier)
  );
  const challenge = base64url(new Uint8Array(hash));
  sessionStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      mode,
      state,
      verifier,
      expiresAt: Date.now() + 5 * 60_000,
      accountId: mode === 'link' ? accountId ?? undefined : undefined,
    } satisfies Pending)
  );
  const url = new URL('/eigent/connect', origin);
  url.searchParams.set('state', state);
  url.searchParams.set('challenge', challenge);
  try {
    const result = await openExternal(url.toString());
    if (!result?.success) throw new Error('Could not open browser');
  } catch (error) {
    sessionStorage.removeItem(STORAGE_KEY);
    throw error;
  }
}

export function consumeClerkHandoff(
  code: string,
  state: string
): (Pending & { code: string }) | null {
  const encoded = sessionStorage.getItem(STORAGE_KEY);
  sessionStorage.removeItem(STORAGE_KEY);
  if (!encoded || !VALUE.test(code) || !VALUE.test(state)) return null;
  try {
    const pending = JSON.parse(encoded) as Pending;
    if (
      (pending.mode !== 'login' && pending.mode !== 'link') ||
      pending.state !== state ||
      !VERIFIER.test(pending.verifier) ||
      !Number.isSafeInteger(pending.expiresAt) ||
      pending.expiresAt <= Date.now() ||
      (pending.mode === 'link' &&
        (typeof pending.accountId !== 'number' ||
          !Number.isSafeInteger(pending.accountId) ||
          pending.accountId <= 0))
    )
      return null;
    return { ...pending, code };
  } catch {
    return null;
  }
}

export function clerkHandoffMatchesAccount(
  pending: Pending,
  token: string | null,
  accountId: number | null
): boolean {
  return pending.mode === 'link'
    ? !!token && pending.accountId === accountId
    : !token;
}
