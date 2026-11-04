import { useEffect, useRef, useState } from 'react';
import QRCode from 'qrcode';

type Encoder = (canvas:HTMLCanvasElement, value:string)=>Promise<void>;
const defaultEncode:Encoder = async (canvas,value)=>{ await QRCode.toCanvas(canvas,value,{errorCorrectionLevel:'M', margin:2, width:240}); };
/** No token in React state, DOM attributes, data URLs, clipboard, files, or logs. */
export function SecureQr({challengeId,epoch,expiresAt,take,encode=defaultEncode,autoShow=false}: {
  challengeId:string; epoch:number; expiresAt:string|null;
  take:(id:string,epoch:number)=>Promise<string>; encode?:Encoder;autoShow?:boolean;
}) {
  const canvas=useRef<HTMLCanvasElement>(null), active=useRef(false), requestRevision=useRef<number|null>(null), revision=useRef(0), deadline=useRef<number|null>(null),showRef=useRef<()=>Promise<void>>(async()=>{});
  const [status,setStatus]=useState<'idle'|'loading'|'shown'|'unavailable'>('idle');
  const currentStatus=useRef(status);
  const present=(next:typeof status)=>{currentStatus.current=next;setStatus(next);};
  useEffect(()=>{
    active.current=true; revision.current++;deadline.current=null;requestRevision.current=null;present('idle');
    const erase=()=>{ revision.current++; const target=canvas.current; if(target){target.width=0;target.height=0;} };
    return ()=>{erase();active.current=false;};
  },[challengeId,epoch]);
  useEffect(()=>{
    // Re-projecting the same challenge can shift its presentation timestamp.
    // Keep the one-shot QR visible and only tighten its existing deadline.
    if(expiresAt){const parsed=Date.parse(expiresAt);const candidate=Number.isFinite(parsed)?parsed:Date.now();deadline.current=deadline.current===null?candidate:Math.min(deadline.current,candidate);}
    if(deadline.current===null)return;
    const timer=setTimeout(()=>{revision.current++;const target=canvas.current;if(target){target.width=0;target.height=0;}if(active.current)present('unavailable');},Math.max(0,deadline.current-Date.now()));
    return ()=>clearTimeout(timer);
  },[challengeId,epoch,expiresAt]);
  const show=async()=>{
    if(requestRevision.current===revision.current || currentStatus.current!=='idle' || !active.current)return;
    if(deadline.current!==null&&deadline.current<=Date.now()){present('unavailable');return;}
    const started=revision.current;requestRevision.current=started;present('loading');
    const temporary=document.createElement('canvas');
    try {
      const value=await take(challengeId,epoch);
      if(!active.current || revision.current!==started) return;
      if(typeof value!=='string'||!value.length||value.length>4096)throw new Error('unavailable');
      await encode(temporary,value);
      if(!active.current || revision.current!==started) return;
      const target=canvas.current; if(!target)throw new Error('unavailable');
      target.width=temporary.width;target.height=temporary.height;
      const context=target.getContext('2d');if(!context)throw new Error('unavailable');
      context.drawImage(temporary,0,0); present('shown');
    } catch { if(active.current&&revision.current===started)present('unavailable'); }
    finally { temporary.width=0;temporary.height=0;if(requestRevision.current===started)requestRevision.current=null; }
  };
  showRef.current=show;
  useEffect(()=>{
    if(!autoShow)return;
    // Wait for a stable mount so React StrictMode's setup/cleanup probe cannot
    // consume the one-shot handoff and discard the only QR.
    const timer=setTimeout(()=>void showRef.current(),0);
    return()=>clearTimeout(timer);
  },[autoShow,challengeId,epoch]);
  return <div><canvas ref={canvas} width={0} height={0} role="img" aria-label="Telegram login QR" />{!autoShow&&<button disabled={status!=='idle'} onClick={()=>void show()}>Show login QR</button>}
    {(status==='loading'||autoShow&&status==='idle')&&<p role="status">Preparing QR code…</p>}
    {status==='shown'&&<p className="muted">On your phone, open Telegram → Settings → Devices → Link Desktop Device, then scan this code.</p>}
    {status==='unavailable'&&<p role="status">QR unavailable or expired. Cancel this login and begin again for a new code.</p>}</div>;
}
