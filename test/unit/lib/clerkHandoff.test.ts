import { webcrypto } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  clerkHandoffAvailable,
  clerkHandoffMatchesAccount,
  consumeClerkHandoff,
  startClerkHandoff,
} from '../../../src/lib/clerkHandoff';

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe('Clerk desktop handoff', () => {
  it('requires a fixed HTTPS site and sends a PKCE challenge without its verifier', async () => {
    vi.stubEnv('VITE_CLERK_HANDOFF_ORIGIN', 'https://robotstaging.hangent.com');
    vi.stubGlobal('crypto', webcrypto);
    expect(clerkHandoffAvailable()).toBe(true);
    let opened = '';
    await startClerkHandoff('login', async (url) => {
      opened = url;
      return { success: true };
    });
    const url = new URL(opened);
    expect(url.origin).toBe('https://robotstaging.hangent.com');
    expect(url.pathname).toBe('/eigent/connect');
    expect(url.searchParams.get('challenge')).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(opened).not.toContain('verifier');
    const state = url.searchParams.get('state')!;
    expect(consumeClerkHandoff('a'.repeat(43), 'wrong-state')).toBeNull();
    expect(consumeClerkHandoff('a'.repeat(43), state)).toBeNull();
  });

  it('consumes a valid matching callback only once', async () => {
    vi.stubEnv('VITE_CLERK_HANDOFF_ORIGIN', 'https://robotstaging.hangent.com');
    vi.stubGlobal('crypto', webcrypto);
    let opened = '';
    await startClerkHandoff(
      'link',
      async (url) => {
        opened = url;
        return { success: true };
      },
      42
    );
    const state = new URL(opened).searchParams.get('state')!;
    const pending = consumeClerkHandoff('a'.repeat(43), state);
    expect(pending?.mode).toBe('link');
    expect(pending?.accountId).toBe(42);
    expect(pending?.verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(clerkHandoffMatchesAccount(pending!, 'token', 42)).toBe(true);
    expect(clerkHandoffMatchesAccount(pending!, 'other-token', 43)).toBe(false);
    expect(clerkHandoffMatchesAccount(pending!, null, 42)).toBe(false);
    expect(consumeClerkHandoff('a'.repeat(43), state)).toBeNull();
  });

  it('requires an account for linking and rejects a missing expiry', async () => {
    vi.stubEnv('VITE_CLERK_HANDOFF_ORIGIN', 'https://robotstaging.hangent.com');
    vi.stubGlobal('crypto', webcrypto);
    await expect(
      startClerkHandoff('link', async () => ({ success: true }))
    ).rejects.toThrow('Eigent account is required');

    let opened = '';
    await startClerkHandoff('login', async (url) => {
      opened = url;
      return { success: true };
    });
    const state = new URL(opened).searchParams.get('state')!;
    const stored = JSON.parse(sessionStorage.getItem('eigent-clerk-handoff')!);
    delete stored.expiresAt;
    sessionStorage.setItem('eigent-clerk-handoff', JSON.stringify(stored));
    expect(consumeClerkHandoff('a'.repeat(43), state)).toBeNull();
  });

  it('does not enable handoff for an insecure site', () => {
    vi.stubEnv('VITE_CLERK_HANDOFF_ORIGIN', 'http://robotstaging.hangent.com');
    expect(clerkHandoffAvailable()).toBe(false);
  });
});
