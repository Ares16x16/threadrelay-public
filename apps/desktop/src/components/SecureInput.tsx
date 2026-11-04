import { useEffect, useRef, useState } from 'react';

type Props = { label: string; kind: 'phone' | 'code' | 'password'; onSubmit: (value: string) => Promise<void>; onCancel: () => void; submitLabel?:string; hint?:string; focusOnMount?:boolean };
/** The DOM element is the only retained input; never copies it into React/global state. */
export function SecureInput({label, kind, onSubmit, onCancel,submitLabel='Continue',hint,focusOnMount=false}: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [pending, setPending] = useState(false);
  const locked = useRef(false);
  const [error, setError] = useState('');
  const clear = () => { if (input.current) input.current.value = ''; };
  useEffect(() => { const node = input.current; return () => { if (node) node.value = ''; }; }, []);
  useEffect(()=>{if(focusOnMount){input.current?.focus();input.current?.scrollIntoView?.({block:'center'});}},[focusOnMount]);
  return <form onSubmit={async event => {
    event.preventDefault();
    if (locked.current || !input.current) { clear(); return; }
    const value = input.current.value;
    clear();
    if (!value) { setError('Enter a value to continue.'); return; }
    locked.current = true; setPending(true); setError('');
    try { await onSubmit(value); }
    catch { setError('Secure submission is unavailable. Start a new attempt or cancel.'); }
    finally { locked.current = false; setPending(false); }
  }}>
    <label>{label}<input ref={input} disabled={pending} type={kind === 'password' ? 'password' : kind==='phone'?'tel':'text'} autoComplete="off" inputMode={kind === 'phone' ? 'tel' : kind==='code'?'numeric':'text'} maxLength={kind==='phone'?64:4096} aria-invalid={!!error} aria-describedby={[hint?'secure-hint':null,error?'secure-error':null].filter(Boolean).join(' ')||undefined} /></label>
    {hint&&<p id="secure-hint" className="muted">{hint}</p>}
    {error && <p id="secure-error" className="error" role="alert">{error}</p>}
    <div className="actions"><button className="primary" type="submit" disabled={pending}>{pending ? 'Submitting…' : submitLabel}</button><button type="button" onClick={() => { clear(); onCancel(); }}>Cancel</button></div>
  </form>;
}
