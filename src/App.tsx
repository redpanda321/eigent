// ========= Copyright 2025-2026 @ Eigent.ai All Rights Reserved. =========
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
// ========= Copyright 2025-2026 @ Eigent.ai All Rights Reserved. =========

import { proxyFetchPost } from '@/api/http';
import { useHost } from '@/host';
import {
  clerkHandoffMatchesAccount,
  consumeClerkHandoff,
} from '@/lib/clerkHandoff';
import { queryClient } from '@/lib/queryClient';
import AppRoutes from '@/routers/index';
import { stackClientApp } from '@/stack/client';
import { StackProvider, StackTheme } from '@stackframe/react';
import { QueryClientProvider } from '@tanstack/react-query';
import React, { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { AppCommandProvider } from './components/Layout/AppCommandProvider';
import { WindowCloseProvider } from './components/Layout/WindowCloseProvider';
import { Toaster } from './components/ui/sonner';
import { useBackgroundTaskProcessor } from './hooks/useBackgroundTaskProcessor';
import { useExecutionSubscription } from './hooks/useExecutionSubscription';
import { useRemoteControlBridge } from './hooks/useRemoteControlBridge';
import { useTriggerTaskExecutor } from './hooks/useTriggerTaskExecutor';
import { hasStackKeys } from './lib';
import { useAuthStore } from './store/authStore';
import { useInstallationStore } from './store/installationStore';

import { useUsageNotices } from '@/hooks/useUsageNotices';

const HAS_STACK_KEYS = hasStackKeys();

function App() {
  useUsageNotices();
  const host = useHost();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { setInitState, setAuth, setModelType, setLocalProxyValue } =
    useAuthStore();
  const { token } = useAuthStore();
  const isBackendReady = useInstallationStore((state) => state.isBackendReady);

  // Subscribe to execution events when user is authenticated
  // Note: Removed triggers.length check to prevent reconnection on every trigger update
  const shouldSubscribe = !!token;
  useExecutionSubscription(shouldSubscribe);
  useBackgroundTaskProcessor();

  // Execute triggered tasks automatically when WebSocket events are received
  useTriggerTaskExecutor();
  // The bridge can receive commands immediately after registration. Do not
  // advertise it until the local Brain can durably persist the command inbox.
  useRemoteControlBridge(token, isBackendReady);

  useEffect(() => {
    const handleClerkHandoff = async (
      _event: unknown,
      payload: { code: string; state: string }
    ) => {
      const pending = consumeClerkHandoff(payload.code, payload.state);
      if (!pending) return;
      const { token: currentToken, user_id: currentUserId } =
        useAuthStore.getState();
      if (!clerkHandoffMatchesAccount(pending, currentToken, currentUserId)) {
        toast.error(t('layout.login-failed-please-try-again'));
        return;
      }
      try {
        const result = await proxyFetchPost(
          `/api/v1/user/clerk/redeem-${pending.mode}`,
          {
            code: pending.code,
            verifier: pending.verifier,
          }
        );
        if (pending.mode === 'link') {
          if (!result?.linked) throw new Error('Clerk account was not linked');
          toast.success(t('setting.clerk-connected'));
          return;
        }
        if (!result?.token || !result?.email || !result?.user_id) {
          throw new Error('Clerk login did not return an Eigent session');
        }
        setModelType('cloud');
        setAuth({
          token: result.token,
          email: result.email,
          username: result.email.split('@')[0],
          user_id: result.user_id,
        });
        setLocalProxyValue(import.meta.env.VITE_USE_LOCAL_PROXY || null);
        navigate('/', { replace: true });
      } catch (error: any) {
        toast.error(
          error?.status === 409
            ? t('layout.clerk-account-not-linked')
            : t('layout.login-failed-please-try-again')
        );
      }
    };

    host?.ipcRenderer?.on('clerk-handoff-received', handleClerkHandoff);
    return () =>
      host?.ipcRenderer?.off('clerk-handoff-received', handleClerkHandoff);
  }, [host, navigate, setAuth, setLocalProxyValue, setModelType, t]);

  useEffect(() => {
    const handleShareCode = (event: any, share_token: string) => {
      navigate({
        pathname: '/',
        search: `?share_token=${encodeURIComponent(share_token)}`,
      });
    };

    //  listen version update notification
    const handleUpdateNotification = (data: {
      type: string;
      currentVersion: string;
      previousVersion: string;
      reason: string;
    }) => {
      console.log('receive version update notification:', data);

      if (data.type === 'version-update') {
        // handle version update logic
        console.log(
          `version from ${data.previousVersion} to ${data.currentVersion}`
        );
        console.log(`update reason: ${data.reason}`);
        setInitState('carousel');
      }
    };

    host?.ipcRenderer?.on('auth-share-token-received', handleShareCode);
    host?.electronAPI?.onUpdateNotification(handleUpdateNotification);

    return () => {
      host?.ipcRenderer?.off('auth-share-token-received', handleShareCode);
      host?.electronAPI?.removeAllListeners('update-notification');
    };
  }, [host, navigate, setInitState]);

  // render wrapper
  const renderWrapper = (children: React.ReactNode) => {
    const content = (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    if (HAS_STACK_KEYS) {
      return (
        <StackProvider app={stackClientApp}>
          <StackTheme>{content}</StackTheme>
          <Toaster style={{ zIndex: 999999, position: 'fixed' }} />
        </StackProvider>
      );
    }
    return (
      <>
        {content}
        <Toaster style={{ zIndex: '999999 !important', position: 'fixed' }} />
      </>
    );
  };

  return renderWrapper(
    <AppCommandProvider>
      <WindowCloseProvider>
        <AppRoutes />
      </WindowCloseProvider>
    </AppCommandProvider>
  );
}

export default App;
