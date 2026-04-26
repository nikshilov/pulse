/**
 * <chat-thread> — message list container. Owns scroll behavior + delegates
 * rendering to <chat-message> (one per message).
 */
import { state } from '../state.js';
import type { Message } from '../state.js';
import type { ChatMessage } from './chat-message.js';

export class ChatThread extends HTMLElement {
  private bound = false;
  private messageNodes = new Map<string, ChatMessage>();

  connectedCallback(): void {
    this.render();
    if (!this.bound) {
      state.addEventListener('messageAppended', this.onAppend as EventListener);
      state.addEventListener('messageUpdated', this.onUpdate as EventListener);
      state.addEventListener(
        'retrievalAttached',
        this.onUpdate as EventListener,
      );
      this.bound = true;
    }
  }

  private onAppend = (e: CustomEvent<Message>): void => {
    this.removeEmptyHint();
    const msg = e.detail;
    const node = document.createElement('chat-message') as ChatMessage;
    node.message = msg;
    this.messageNodes.set(msg.id, node);
    this.appendChild(node);
    this.scrollToEnd();
  };

  private onUpdate = (e: CustomEvent<Message>): void => {
    const msg = e.detail;
    const node = this.messageNodes.get(msg.id);
    if (node) {
      node.message = msg;
      this.scrollToEnd();
    }
  };

  private render(): void {
    if (state.messages.length === 0) {
      this.innerHTML = `<div class="empty">say something — pulse will retrieve memory live as you go.</div>`;
    }
  }

  private removeEmptyHint(): void {
    const empty = this.querySelector('.empty');
    if (empty) empty.remove();
  }

  private scrollToEnd(): void {
    requestAnimationFrame(() => {
      this.scrollTop = this.scrollHeight;
    });
  }
}
