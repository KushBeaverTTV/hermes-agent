import { act, cleanup, render, screen } from '@testing-library/react'
import { atom, type WritableAtom } from 'nanostores'
import { createElement, Fragment, type ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import type { ChatMessage } from '@/lib/chat-messages'

import { type SessionView, SessionViewProvider } from './session-view'

import { ChatView } from './index'

vi.mock('@assistant-ui/react', () => ({
  AssistantRuntimeProvider: ({ children, runtime }: { children: ReactNode; runtime: ChatMessage[] }) =>
    createElement(
      Fragment,
      null,
      createElement('output', { 'data-testid': 'runtime-messages' }, runtime.map(message => message.id).join(',')),
      children
    )
}))
vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data: undefined }) }))
vi.mock('@/components/assistant-ui/thread', () => ({ Thread: () => null }))
vi.mock('@/components/Backdrop', () => ({ Backdrop: () => null }))
vi.mock('@/components/chat/vibe-hearts', () => ({ COMPOSER_HEART_CONFIG: {}, HeartField: () => null }))
vi.mock('@/components/prompt-overlays', () => ({ PromptOverlays: () => null }))
vi.mock('@/i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
vi.mock('@/lib/query-client', () => ({ queryClient: { invalidateQueries: vi.fn() } }))
vi.mock('@/lib/incremental-external-store-runtime', () => ({
  useIncrementalExternalStoreRuntime: ({ messageRepository }: { messageRepository: ChatMessage[] }) => messageRepository
}))
vi.mock('./chat-drop-overlay', () => ({ ChatDropOverlay: () => null }))
vi.mock('./chat-swap-overlay', () => ({ ChatSwapOverlay: () => null }))
vi.mock('./composer', () => ({ ChatBar: () => null, ChatBarFallback: () => null }))
vi.mock('./composer/scope', () => ({ useComposerScope: () => ({ target: 'test' }) }))
vi.mock('./hooks/use-file-drop-zone', () => ({
  useFileDropZone: () => ({ dragKind: null, dropHandlers: {} })
}))
vi.mock('./runtime-repository', () => ({ useRuntimeMessageRepository: (messages: ChatMessage[]) => messages }))
vi.mock('./scroll-to-bottom-button', () => ({ ScrollToBottomButton: () => null }))

const message = (id: string): ChatMessage => ({ id, parts: [{ text: id, type: 'text' }], role: 'assistant' })

function sessionView(
  id: string,
  messages: ChatMessage[]
): SessionView & { $messages: WritableAtom<ChatMessage[]> } {
  return {
    kind: 'tile',
    $awaitingResponse: atom(false),
    $busy: atom(false),
    $cwd: atom(''),
    $fast: atom(false),
    $lastVisibleIsUser: atom(false),
    $messages: atom(messages),
    $messagesEmpty: atom(messages.length === 0),
    $model: atom(''),
    $provider: atom(''),
    $reasoningEffort: atom(''),
    $runtimeId: atom(`runtime-${id}`),
    $storedId: atom(id)
  }
}

const chatProps = {
  gateway: null,
  onAddContextRef: vi.fn(),
  onAddUrl: vi.fn(),
  onAttachDroppedItems: vi.fn(),
  onAttachImageBlob: vi.fn(),
  onBranchInNewChat: vi.fn(),
  onCancel: vi.fn(),
  onDeleteSelectedSession: vi.fn(),
  onEdit: vi.fn(),
  onPasteClipboardImage: vi.fn(),
  onPickFiles: vi.fn(),
  onPickFolders: vi.fn(),
  onPickImages: vi.fn(),
  onReload: vi.fn(),
  onRemoveAttachment: vi.fn(),
  onRetryResume: vi.fn(),
  onSteer: vi.fn(),
  onSubmit: vi.fn(),
  onThreadMessagesChange: vi.fn(),
  onToggleSelectedPin: vi.fn()
}

function ChatHarness({ view, visible }: { view: SessionView; visible: boolean }) {
  return (
    <MemoryRouter>
      <PaneVisibleContext.Provider value={visible}>
        <SessionViewProvider value={view}>
          <ChatView {...chatProps} />
        </SessionViewProvider>
      </PaneVisibleContext.Provider>
    </MemoryRouter>
  )
}

afterEach(cleanup)

describe('ChatView message visibility', () => {
  it('synchronizes an already-loaded session when the message atom changes', () => {
    const first = sessionView('first', [message('first-message')])
    const second = sessionView('second', [message('second-message')])
    const { rerender } = render(<ChatHarness view={first} visible={false} />)

    expect(screen.getByTestId('runtime-messages').textContent).toBe('first-message')

    rerender(<ChatHarness view={second} visible={false} />)

    expect(screen.getByTestId('runtime-messages').textContent).toBe('second-message')
  })

  it('freezes hidden streaming updates and catches up when the pane becomes visible', () => {
    const view = sessionView('first', [message('first-message')])
    const { rerender } = render(<ChatHarness view={view} visible />)

    act(() => view.$messages.set([message('visible-update')]))
    expect(screen.getByTestId('runtime-messages').textContent).toBe('visible-update')

    rerender(<ChatHarness view={view} visible={false} />)
    act(() => view.$messages.set([message('hidden-update')]))
    expect(screen.getByTestId('runtime-messages').textContent).toBe('visible-update')

    rerender(<ChatHarness view={view} visible />)
    expect(screen.getByTestId('runtime-messages').textContent).toBe('hidden-update')
  })
})
