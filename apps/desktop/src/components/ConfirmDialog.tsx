import { useEffect, useId, useRef, type ReactNode } from 'react';
export function ConfirmDialog({title, children, confirmLabel, disabled = false, onConfirm, onCancel}: {title:string; children:ReactNode; confirmLabel:string; disabled?:boolean; onConfirm:()=>void; onCancel:()=>void}) {
  const dialog = useRef<HTMLDivElement>(null);
  const titleId = useId(), descriptionId = useId();
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const contain = (event: FocusEvent) => {
      if (event.target instanceof Node && !dialog.current?.contains(event.target)) cancel.current?.focus();
    };
    cancel.current?.focus();
    document.addEventListener('focusin', contain);
    return () => { document.removeEventListener('focusin', contain); previous?.focus(); };
  }, []);
  return <div className="dialog-backdrop"><div ref={dialog} className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} onKeyDown={event => {
    if (event.key === 'Escape') { event.preventDefault(); onCancel(); }
    if (event.key !== 'Tab') return;
    const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]') ?? []);
    if (!controls.length) { event.preventDefault(); return; }
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }}><h2 id={titleId}>{title}</h2><div className="dialog-content" id={descriptionId}>{children}</div><div className="actions"><button ref={cancel} type="button" onClick={onCancel}>Cancel</button><button className="primary" type="button" disabled={disabled} onClick={onConfirm}>{confirmLabel}</button></div></div></div>;
}
