/**
 * <chat-message> — a single chat bubble. Setter `.message = ...` triggers
 * re-render. For assistant messages, also includes a <memory-row> child
 * once retrieval meta is attached.
 */
import type { Message } from '../state.js';
import type { MemoryRow } from './memory-row.js';

export class ChatMessage extends HTMLElement {
  private _message: Message | null = null;
  private bubble: HTMLDivElement | null = null;
  private memoryNode: MemoryRow | null = null;

  set message(m: Message) {
    this._message = m;
    if (!this.bubble) {
      this.renderShell();
    }
    this.update();
  }

  get message(): Message | null {
    return this._message;
  }

  private renderShell(): void {
    if (!this._message) return;
    this.setAttribute('role', this._message.role);
    this.innerHTML = `<div class="bubble"></div>`;
    this.bubble = this.querySelector('.bubble');
  }

  private update(): void {
    if (!this._message || !this.bubble) return;
    // Plain text for now; markdown rendering is Phase I.4 polish
    this.bubble.textContent = this._message.text || '…';

    // Streaming-cursor flag drives the blinking pipe via CSS
    if (this._message.streaming) {
      this.dataset.streaming = 'true';
    } else {
      delete this.dataset.streaming;
    }

    // Inject <memory-row> for assistant messages once retrieval attached
    if (
      this._message.role === 'assistant' &&
      this._message.retrieval &&
      !this.memoryNode
    ) {
      const node = document.createElement('memory-row') as MemoryRow;
      node.retrieval = this._message.retrieval;
      this.appendChild(node);
      this.memoryNode = node;
    }
  }
}
