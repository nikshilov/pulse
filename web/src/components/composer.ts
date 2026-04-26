/**
 * <composer-bar> — text input + send button. Dispatches `composer:send`
 * on document scope; orchestrator handles the rest.
 */

export class ComposerBar extends HTMLElement {
  private textarea: HTMLTextAreaElement | null = null;
  private button: HTMLButtonElement | null = null;
  private busy = false;

  connectedCallback(): void {
    this.render();
    document.addEventListener('orchestrator:start', this.onStart);
    document.addEventListener('orchestrator:done', this.onDone);
  }

  disconnectedCallback(): void {
    document.removeEventListener('orchestrator:start', this.onStart);
    document.removeEventListener('orchestrator:done', this.onDone);
  }

  private onStart = (): void => {
    this.busy = true;
    if (this.button) this.button.disabled = true;
  };

  private onDone = (): void => {
    this.busy = false;
    if (this.button) this.button.disabled = false;
    this.textarea?.focus();
  };

  private render(): void {
    this.innerHTML = `
      <form>
        <textarea rows="1" placeholder="Сообщение… (enter — отправить, shift+enter — новая строка)"></textarea>
        <button type="submit">send</button>
      </form>
    `;
    const form = this.querySelector('form') as HTMLFormElement;
    this.textarea = this.querySelector('textarea');
    this.button = this.querySelector('button');

    form.addEventListener('submit', (e) => {
      e.preventDefault();
      this.send();
    });

    this.textarea?.addEventListener('input', () => {
      this.autoresize();
    });

    this.textarea?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.send();
      }
    });
  }

  private autoresize(): void {
    if (!this.textarea) return;
    this.textarea.style.height = 'auto';
    this.textarea.style.height = Math.min(this.textarea.scrollHeight, 200) + 'px';
  }

  private send(): void {
    if (this.busy || !this.textarea) return;
    const text = this.textarea.value.trim();
    if (!text) return;
    this.textarea.value = '';
    this.autoresize();
    document.dispatchEvent(
      new CustomEvent('composer:send', { detail: { text } }),
    );
  }
}
